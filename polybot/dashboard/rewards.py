"""Read-only reader for *real* Polymarket liquidity-reward figures.

This is dashboard telemetry only — it never places, cancels, or influences orders, and it
lives entirely outside the trading loop. It reuses the same authenticated py-clob-client-v2
the live bot builds (via :class:`LiveExecutor`) to call read-only reward endpoints:

* ``/rewards/user/total`` — the user's real earned USDC for a UTC day (the truth, vs. the
  paper engine's heuristic ``estimated_reward_usdc``).
* ``/rewards/user/percentages`` — the user's current share of each market's reward pool,
  which combined with each market's ``rewards_rate_per_day`` gives a real $/day run-rate.

Everything is wrapped so a credential/network failure degrades to ``enabled: False`` and
never propagates into the dashboard request or the bot.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

from polybot.config import Settings
from polybot.storage import Storage


def _parse_earnings(value: Any) -> float | None:
    """Best-effort extraction of a USDC amount from the rewards endpoints' loose shapes."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        return n / 1_000_000 if n > 100_000 else n
    if isinstance(value, str):
        try:
            return _parse_earnings(float(value))
        except ValueError:
            return None
    if isinstance(value, dict):
        for key in (
            "earnings",
            "total",
            "total_earnings",
            "amount",
            "value",
            "reward",
            "rewards",
            "balance",
            "balance_allowance",
        ):
            if key in value:
                return _parse_earnings(value[key])
        return None
    if isinstance(value, list):
        total = 0.0
        seen = False
        for item in value:
            parsed = _parse_earnings(item)
            if parsed is not None:
                total += parsed
                seen = True
        return total if seen else None
    return None


def _market_rates(storage: Storage) -> dict[str, float]:
    with storage.connect() as db:
        rows = db.execute(
            "SELECT condition_id, rewards_rate_per_day FROM reward_markets"
        ).fetchall()
    return {row["condition_id"]: float(row["rewards_rate_per_day"] or 0.0) for row in rows}


class RewardsReader:
    """Caches an authed client and the latest real-reward figures (TTL ~45s)."""

    def __init__(self, settings: Settings, ttl_seconds: float = 45.0) -> None:
        self.settings = settings
        self.ttl = ttl_seconds
        self._client: Any = None
        self._v2: Any = None
        self._init_failed = False
        self._lock = threading.Lock()
        self._cache: dict[str, Any] | None = None
        self._cache_ts = 0.0

    def _ensure_client(self) -> Any:
        if self._client is not None or self._init_failed:
            return self._client
        try:
            from polybot.execution.live import LiveExecutor

            client, v2, _creds = LiveExecutor(self.settings)._build_py_clob_client()
            self._client = client
            self._v2 = v2
        except Exception:  # noqa: BLE001 — no creds / live deps / network: stay disabled
            self._init_failed = True
            self._client = None
        return self._client

    def snapshot(self, storage: Storage) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if self._cache is not None and now - self._cache_ts < self.ttl:
                return self._cache
        result = self._compute(storage)
        with self._lock:
            self._cache = result
            self._cache_ts = time.time()
        return result

    def _compute(self, storage: Storage) -> dict[str, Any]:
        out: dict[str, Any] = {
            "enabled": False,
            "today_usdc": None,
            "live_rate_per_day": None,
            "balance_usdc": None,
            "by_market": [],
            "date": None,
            "error": None,
        }
        client = self._ensure_client()
        if client is None:
            out["error"] = "live credentials unavailable"
            return out
        out["enabled"] = True
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        out["date"] = date

        try:
            out["today_usdc"] = _parse_earnings(client.get_total_earnings_for_user_for_day(date))
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"earnings: {exc}"

        try:
            params = self._v2.BalanceAllowanceParams(
                asset_type=self._v2.AssetType.COLLATERAL,
                signature_type=self.settings.wallet_signature_type,
            )
            out["balance_usdc"] = _parse_earnings(client.get_balance_allowance(params))
        except Exception:  # noqa: BLE001 — balance is best-effort
            pass

        try:
            percentages = client.get_reward_percentages() or {}
            rates = _market_rates(storage)
            rate_total = 0.0
            by_market: list[dict[str, Any]] = []
            for condition_id, pct in percentages.items():
                share = float(pct) / 100.0
                rate_per_day = rates.get(condition_id, 0.0)
                usdc_per_day = share * rate_per_day
                rate_total += usdc_per_day
                by_market.append(
                    {
                        "condition_id": condition_id,
                        "pct": float(pct),
                        "rate_per_day": rate_per_day,
                        "usdc_per_day": usdc_per_day,
                    }
                )
            out["live_rate_per_day"] = rate_total
            out["by_market"] = sorted(
                by_market, key=lambda x: x["usdc_per_day"], reverse=True
            )
        except Exception as exc:  # noqa: BLE001
            if out["error"] is None:
                out["error"] = f"percentages: {exc}"

        return out

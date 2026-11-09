from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from polybot.rewards.models import RewardPair


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, default=str, separators=(",", ":"))


def _rows(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _annotate_reward_inventory_risk(
    inventory: list[dict[str, Any]], max_loss_usdc: float
) -> None:
    by_condition: dict[str, list[dict[str, Any]]] = {}
    for row in inventory:
        by_condition.setdefault(str(row["condition_id"]), []).append(row)
        shares = float(row.get("shares") or 0.0)
        row["matched_shares"] = 0.0
        row["unhedged_shares"] = shares
        row["unhedged_pnl"] = row.get("liquidation_pnl") or 0.0
        liq_pnl = row.get("liquidation_pnl")
        hedge_pnl = row.get("best_hedge_pnl")
        risk_candidates = [
            float(value) for value in (liq_pnl, hedge_pnl) if value is not None
        ]
        risk_pnl = min(risk_candidates) if risk_candidates else None
        row["risk_pnl"] = risk_pnl
        row["loss_to_stop"] = (
            max_loss_usdc + risk_pnl if risk_pnl is not None else None
        )
    for items in by_condition.values():
        if len(items) < 2:
            continue
        first, second = items[0], items[1]
        matched_shares = min(float(first.get("shares") or 0.0), float(second.get("shares") or 0.0))
        if matched_shares <= 1e-12:
            continue
        for row in (first, second):
            shares = float(row.get("shares") or 0.0)
            avg_price = float(row.get("avg_price") or 0.0)
            mark_price = row.get("mark_price")
            unhedged_shares = max(0.0, shares - matched_shares)
            row["matched_shares"] = matched_shares
            row["unhedged_shares"] = unhedged_shares
            row["unhedged_pnl"] = (
                (float(mark_price) - avg_price) * unhedged_shares
                if mark_price is not None
                else 0.0
            )


def _reward_matched_bundles(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_condition: dict[str, list[dict[str, Any]]] = {}
    for row in inventory:
        by_condition.setdefault(str(row["condition_id"]), []).append(row)
    bundles: list[dict[str, Any]] = []
    for items in by_condition.values():
        if len(items) < 2:
            continue
        first, second = items[0], items[1]
        matched_shares = min(float(first.get("shares") or 0.0), float(second.get("shares") or 0.0))
        if matched_shares <= 1e-12:
            continue
        bundle_cost = float(first.get("avg_price") or 0.0) + float(
            second.get("avg_price") or 0.0
        )
        bundles.append(
            {
                "condition_id": first["condition_id"],
                "question": first.get("question", ""),
                "outcome_a": first.get("outcome", ""),
                "outcome_b": second.get("outcome", ""),
                "matched_shares": matched_shares,
                "bundle_cost": bundle_cost,
                "matched_pnl": (1.0 - bundle_cost) * matched_shares,
            }
        )
    return sorted(bundles, key=lambda row: abs(row["matched_pnl"]), reverse=True)


class Storage:
    """SQLite storage for reward market making.

    Tables are prefixed with ``reward_``, so a database written by an older build keeps its
    other tables untouched instead of colliding with these.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser().resolve()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS reward_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    status TEXT NOT NULL,
                    requested_markets INTEGER NOT NULL,
                    size_shares REAL NOT NULL,
                    config_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reward_markets (
                    condition_id TEXT PRIMARY KEY,
                    question TEXT NOT NULL,
                    event_id TEXT,
                    event_slug TEXT,
                    outcome_a TEXT NOT NULL,
                    outcome_b TEXT NOT NULL,
                    token_a TEXT NOT NULL,
                    token_b TEXT NOT NULL,
                    volume_24hr REAL NOT NULL DEFAULT 0,
                    market_spread REAL,
                    rewards_min_size REAL NOT NULL DEFAULT 0,
                    rewards_max_spread REAL,
                    rewards_rate_per_day REAL NOT NULL DEFAULT 0,
                    tick_size REAL NOT NULL DEFAULT 0.01,
                    min_order_size REAL NOT NULL DEFAULT 1,
                    minimum_order_age_seconds INTEGER NOT NULL DEFAULT 0,
                    fee_rate REAL NOT NULL DEFAULT 0,
                    fees_enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reward_run_markets (
                    run_id INTEGER NOT NULL,
                    condition_id TEXT NOT NULL,
                    PRIMARY KEY(run_id, condition_id)
                );
                CREATE TABLE IF NOT EXISTS reward_quotes (
                    quote_uid TEXT PRIMARY KEY,
                    run_id INTEGER NOT NULL,
                    condition_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    leg TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    size REAL NOT NULL,
                    distance_from_midpoint REAL NOT NULL,
                    max_spread REAL NOT NULL,
                    score_weight REAL NOT NULL DEFAULT 0,
                    post_only_safe INTEGER NOT NULL DEFAULT 0,
                    size_eligible INTEGER NOT NULL DEFAULT 0,
                    locally_scoring INTEGER NOT NULL DEFAULT 0,
                    lifecycle_state TEXT NOT NULL DEFAULT 'open',
                    cancel_requested_at REAL,
                    cancel_ack_at REAL,
                    cancel_reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    opened_at REAL NOT NULL,
                    closed_at REAL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reward_quotes_run_status
                    ON reward_quotes(run_id, status);
                CREATE INDEX IF NOT EXISTS idx_reward_quotes_run_condition_status
                    ON reward_quotes(run_id, condition_id, status);
                CREATE TABLE IF NOT EXISTS reward_quote_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    quote_uid TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    size REAL NOT NULL,
                    midpoint REAL NOT NULL,
                    best_bid REAL NOT NULL,
                    best_ask REAL NOT NULL,
                    spread REAL NOT NULL,
                    distance_from_midpoint REAL NOT NULL,
                    max_spread REAL NOT NULL,
                    depth_ahead REAL,
                    age_seconds REAL NOT NULL,
                    post_only_safe INTEGER NOT NULL DEFAULT 0,
                    size_eligible INTEGER NOT NULL DEFAULT 0,
                    within_spread INTEGER NOT NULL DEFAULT 0,
                    locally_scoring INTEGER NOT NULL DEFAULT 0,
                    score_weight REAL NOT NULL DEFAULT 0,
                    lifecycle_state TEXT NOT NULL DEFAULT 'open',
                    estimated_reward_usdc REAL NOT NULL DEFAULT 0,
                    local_ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reward_snapshots_run_quote_ts
                    ON reward_quote_snapshots(run_id, quote_uid, local_ts);
                CREATE INDEX IF NOT EXISTS idx_reward_snapshots_quote_ts
                    ON reward_quote_snapshots(quote_uid, local_ts DESC);
                CREATE TABLE IF NOT EXISTS reward_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    quote_uid TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    side TEXT NOT NULL,
                    trade_price REAL NOT NULL,
                    quote_price REAL NOT NULL,
                    size REAL NOT NULL,
                    lifecycle_state TEXT NOT NULL DEFAULT 'open',
                    fill_reason TEXT NOT NULL DEFAULT '',
                    maker_fee_equivalent_usdc REAL NOT NULL DEFAULT 0,
                    maker_rebate_conservative_usdc REAL NOT NULL DEFAULT 0,
                    maker_rebate_base_usdc REAL NOT NULL DEFAULT 0,
                    maker_rebate_optimistic_usdc REAL NOT NULL DEFAULT 0,
                    exchange_ts_ms INTEGER,
                    local_ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reward_fills_run_ts
                    ON reward_fills(run_id, local_ts DESC);
                CREATE TABLE IF NOT EXISTS reward_inventory (
                    run_id INTEGER NOT NULL,
                    condition_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    inventory_uid TEXT,
                    state TEXT NOT NULL DEFAULT 'open_inventory',
                    shares REAL NOT NULL,
                    avg_price REAL NOT NULL,
                    mark_price REAL,
                    unrealized_pnl REAL,
                    liquidation_pnl REAL,
                    reward_credit_usdc REAL NOT NULL DEFAULT 0,
                    best_hedge_price REAL,
                    best_hedge_bundle_cost REAL,
                    best_hedge_pnl REAL,
                    passive_exit_quote_uid TEXT,
                    passive_exit_price REAL,
                    opened_at REAL NOT NULL DEFAULT 0,
                    closed_at REAL,
                    close_mode TEXT,
                    closed_pnl REAL,
                    reward_adjusted_pnl REAL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(run_id, condition_id, token_id)
                );
                CREATE TABLE IF NOT EXISTS reward_inventory_marks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    inventory_uid TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    state TEXT NOT NULL,
                    shares REAL NOT NULL,
                    avg_price REAL NOT NULL,
                    mark_price REAL,
                    liquidation_pnl REAL,
                    reward_credit_usdc REAL NOT NULL DEFAULT 0,
                    best_hedge_price REAL,
                    best_hedge_bundle_cost REAL,
                    best_hedge_pnl REAL,
                    passive_exit_price REAL,
                    local_ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reward_inventory_marks_run_uid_ts
                    ON reward_inventory_marks(run_id, inventory_uid, local_ts);
                CREATE TABLE IF NOT EXISTS reward_inventory_closures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    inventory_uid TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    close_mode TEXT NOT NULL,
                    shares REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    hedge_price REAL,
                    bundle_cost REAL,
                    realized_pnl REAL NOT NULL,
                    reward_credit_usdc REAL NOT NULL DEFAULT 0,
                    reward_adjusted_pnl REAL NOT NULL,
                    risk_reason TEXT NOT NULL DEFAULT '',
                    trigger_pnl REAL,
                    exit_pnl REAL,
                    liquidation_pnl_at_trigger REAL,
                    hedge_pnl_at_trigger REAL,
                    planned_max_loss_usdc REAL,
                    stop_slippage_usdc REAL NOT NULL DEFAULT 0,
                    exit_route TEXT NOT NULL DEFAULT '',
                    closed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reward_closures_run_mode
                    ON reward_inventory_closures(run_id, close_mode);
                CREATE INDEX IF NOT EXISTS idx_reward_closures_run_closed
                    ON reward_inventory_closures(run_id, closed_at DESC);
                CREATE TABLE IF NOT EXISTS reward_metrics (
                    run_id INTEGER NOT NULL,
                    metric_key TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(run_id, metric_key)
                );
                CREATE TABLE IF NOT EXISTS reward_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reward_events_run_created
                    ON reward_events(run_id, created_at DESC);
                """
            )
            self._migrate_columns(db)

    @staticmethod
    def _migrate_columns(db: sqlite3.Connection) -> None:
        additions = {
            "reward_inventory": {
                "inventory_uid": "TEXT",
                "state": "TEXT NOT NULL DEFAULT 'open_inventory'",
                "liquidation_pnl": "REAL",
                "reward_credit_usdc": "REAL NOT NULL DEFAULT 0",
                "best_hedge_price": "REAL",
                "best_hedge_bundle_cost": "REAL",
                "best_hedge_pnl": "REAL",
                "passive_exit_quote_uid": "TEXT",
                "passive_exit_price": "REAL",
                "opened_at": "REAL NOT NULL DEFAULT 0",
                "closed_at": "REAL",
                "close_mode": "TEXT",
                "closed_pnl": "REAL",
                "reward_adjusted_pnl": "REAL",
            },
            "reward_markets": {
                "fee_rate": "REAL NOT NULL DEFAULT 0",
                "fees_enabled": "INTEGER NOT NULL DEFAULT 0",
                "game_start_time": "TEXT",
                "one_day_price_change": "REAL NOT NULL DEFAULT 0",
                "end_date": "TEXT",
            },
            "reward_quotes": {
                "lifecycle_state": "TEXT NOT NULL DEFAULT 'open'",
                "cancel_requested_at": "REAL",
                "cancel_ack_at": "REAL",
                "cancel_reason": "TEXT NOT NULL DEFAULT ''",
            },
            "reward_quote_snapshots": {
                "lifecycle_state": "TEXT NOT NULL DEFAULT 'open'",
                "depth_ahead": "REAL",
            },
            "reward_fills": {
                "lifecycle_state": "TEXT NOT NULL DEFAULT 'open'",
                "fill_reason": "TEXT NOT NULL DEFAULT ''",
                "maker_fee_equivalent_usdc": "REAL NOT NULL DEFAULT 0",
                "maker_rebate_conservative_usdc": "REAL NOT NULL DEFAULT 0",
                "maker_rebate_base_usdc": "REAL NOT NULL DEFAULT 0",
                "maker_rebate_optimistic_usdc": "REAL NOT NULL DEFAULT 0",
            },
            "reward_inventory_closures": {
                "risk_reason": "TEXT NOT NULL DEFAULT ''",
                "trigger_pnl": "REAL",
                "exit_pnl": "REAL",
                "liquidation_pnl_at_trigger": "REAL",
                "hedge_pnl_at_trigger": "REAL",
                "planned_max_loss_usdc": "REAL",
                "stop_slippage_usdc": "REAL NOT NULL DEFAULT 0",
                "exit_route": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in additions.items():
            existing = {
                str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, definition in columns.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def create_reward_run(
        self,
        requested_markets: int,
        size_shares: float,
        config: dict[str, Any],
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO reward_runs
                    (started_at, status, requested_markets, size_shares, config_json)
                VALUES (?, 'running', ?, ?, ?)
                """,
                (time.time(), requested_markets, size_shares, _json(config)),
            )
            return int(cursor.lastrowid)

    def finish_reward_run(self, run_id: int, status: str = "stopped") -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE reward_runs SET ended_at=?, status=? WHERE id=?",
                (time.time(), status, run_id),
            )

    def latest_reward_run_id(self) -> int | None:
        with self.connect() as db:
            row = db.execute("SELECT id FROM reward_runs ORDER BY id DESC LIMIT 1").fetchone()
            return int(row["id"]) if row else None

    def upsert_reward_markets(self, run_id: int, pairs: list[RewardPair]) -> None:
        now = time.time()
        with self.connect() as db:
            db.executemany(
                """
                INSERT INTO reward_markets (
                    condition_id, question, event_id, event_slug, outcome_a, outcome_b,
                    token_a, token_b, volume_24hr, market_spread, rewards_min_size,
                    rewards_max_spread, rewards_rate_per_day, tick_size, min_order_size,
                    minimum_order_age_seconds, fee_rate, fees_enabled,
                    game_start_time, one_day_price_change, end_date, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(condition_id) DO UPDATE SET
                    question=excluded.question,
                    event_id=excluded.event_id,
                    event_slug=excluded.event_slug,
                    outcome_a=excluded.outcome_a,
                    outcome_b=excluded.outcome_b,
                    token_a=excluded.token_a,
                    token_b=excluded.token_b,
                    volume_24hr=excluded.volume_24hr,
                    market_spread=excluded.market_spread,
                    rewards_min_size=excluded.rewards_min_size,
                    rewards_max_spread=excluded.rewards_max_spread,
                    rewards_rate_per_day=excluded.rewards_rate_per_day,
                    tick_size=excluded.tick_size,
                    min_order_size=excluded.min_order_size,
                    minimum_order_age_seconds=excluded.minimum_order_age_seconds,
                    fee_rate=excluded.fee_rate,
                    fees_enabled=excluded.fees_enabled,
                    game_start_time=excluded.game_start_time,
                    one_day_price_change=excluded.one_day_price_change,
                    end_date=excluded.end_date,
                    updated_at=excluded.updated_at
                """,
                [
                    (
                        pair.condition_id,
                        pair.question,
                        pair.event_id,
                        pair.event_slug,
                        pair.outcome_a,
                        pair.outcome_b,
                        pair.token_a,
                        pair.token_b,
                        pair.volume_24hr,
                        pair.market_spread,
                        pair.rewards_min_size,
                        pair.rewards_max_spread,
                        pair.rewards_rate_per_day,
                        float(pair.tick_size),
                        pair.min_order_size,
                        pair.minimum_order_age_seconds,
                        pair.fee_rate,
                        int(pair.fees_enabled),
                        pair.game_start_time.isoformat() if pair.game_start_time else None,
                        pair.one_day_price_change,
                        pair.end_date.isoformat() if pair.end_date else None,
                        now,
                    )
                    for pair in pairs
                ],
            )
            db.execute("DELETE FROM reward_run_markets WHERE run_id=?", (run_id,))
            db.executemany(
                "INSERT INTO reward_run_markets (run_id, condition_id) VALUES (?, ?)",
                [(run_id, pair.condition_id) for pair in pairs],
            )

    def insert_reward_quote(self, quote: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO reward_quotes (
                    quote_uid, run_id, condition_id, token_id, outcome, leg, side,
                    price, size, distance_from_midpoint, max_spread, score_weight,
                    post_only_safe, size_eligible, locally_scoring, lifecycle_state,
                    cancel_requested_at, cancel_ack_at, cancel_reason, status, reason,
                    opened_at, closed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quote["quote_uid"],
                    quote["run_id"],
                    quote["condition_id"],
                    quote["token_id"],
                    quote["outcome"],
                    quote["leg"],
                    quote["side"],
                    quote["price"],
                    quote["size"],
                    quote["distance_from_midpoint"],
                    quote["max_spread"],
                    quote.get("score_weight", 0.0),
                    int(bool(quote.get("post_only_safe"))),
                    int(bool(quote.get("size_eligible"))),
                    int(bool(quote.get("locally_scoring"))),
                    quote.get("lifecycle_state", quote.get("status", "open")),
                    quote.get("cancel_requested_at"),
                    quote.get("cancel_ack_at"),
                    quote.get("cancel_reason", ""),
                    quote["status"],
                    quote.get("reason", ""),
                    quote["opened_at"],
                    quote.get("closed_at"),
                    quote.get("updated_at", time.time()),
                ),
            )

    def close_reward_quote(
        self, quote_uid: str, *, status: str, reason: str = "", closed_at: float | None = None
    ) -> None:
        now = time.time()
        with self.connect() as db:
            db.execute(
                """
                UPDATE reward_quotes
                SET status=?, lifecycle_state=?, reason=?, closed_at=?, updated_at=?
                WHERE quote_uid=?
                """,
                (status, status, reason, closed_at or now, now, quote_uid),
            )

    def close_reward_quotes(
        self,
        quote_uids: list[str],
        *,
        status: str,
        reason: str = "",
        closed_at: float | None = None,
    ) -> None:
        if not quote_uids:
            return
        now = time.time()
        closed = closed_at or now
        with self.connect() as db:
            db.executemany(
                """
                UPDATE reward_quotes
                SET status=?, lifecycle_state=?, reason=?, closed_at=?, updated_at=?
                WHERE quote_uid=?
                """,
                [(status, status, reason, closed, now, quote_uid) for quote_uid in quote_uids],
            )

    def close_open_reward_quotes_for_run(
        self,
        run_id: int,
        *,
        status: str,
        reason: str = "",
        closed_at: float | None = None,
    ) -> None:
        now = time.time()
        with self.connect() as db:
            db.execute(
                """
                UPDATE reward_quotes
                SET status=?, lifecycle_state=?, reason=?, closed_at=?, updated_at=?
                WHERE run_id=? AND status IN ('open', 'cancel_requested')
                """,
                (status, status, reason, closed_at or now, now, run_id),
            )

    def insert_reward_quote_snapshots(self, snapshots: list[dict[str, Any]]) -> None:
        if not snapshots:
            return
        with self.connect() as db:
            db.executemany(
                """
                INSERT INTO reward_quote_snapshots (
                    run_id, quote_uid, condition_id, token_id, outcome, side, price, size,
                    midpoint, best_bid, best_ask, spread, distance_from_midpoint, max_spread,
                    depth_ahead, age_seconds, post_only_safe, size_eligible, within_spread,
                    locally_scoring, score_weight, lifecycle_state, estimated_reward_usdc, local_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["run_id"],
                        row["quote_uid"],
                        row["condition_id"],
                        row["token_id"],
                        row["outcome"],
                        row["side"],
                        row["price"],
                        row["size"],
                        row["midpoint"],
                        row["best_bid"],
                        row["best_ask"],
                        row["spread"],
                        row["distance_from_midpoint"],
                        row["max_spread"],
                        row.get("depth_ahead"),
                        row["age_seconds"],
                        int(bool(row.get("post_only_safe"))),
                        int(bool(row.get("size_eligible"))),
                        int(bool(row.get("within_spread"))),
                        int(bool(row.get("locally_scoring"))),
                        row.get("score_weight", 0.0),
                        row.get("lifecycle_state", "open"),
                        row.get("estimated_reward_usdc", 0.0),
                        row["local_ts"],
                    )
                    for row in snapshots
                ],
            )

    def prune_reward_snapshots(self, run_id: int, before_ts: float) -> int:
        """Drop quote snapshots and closed quote rows older than ``before_ts``.

        Keeps the reward DB bounded during long runs; open/cancel-pending quote
        rows are always retained so the live dashboard stays accurate.
        """
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM reward_quote_snapshots WHERE run_id=? AND local_ts < ?",
                (run_id, before_ts),
            )
            pruned = cursor.rowcount or 0
            db.execute(
                """
                DELETE FROM reward_quotes
                WHERE run_id=? AND updated_at < ?
                  AND status NOT IN ('open', 'cancel_requested')
                """,
                (run_id, before_ts),
            )
        return pruned

    def insert_reward_fill(self, fill: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO reward_fills (
                    run_id, quote_uid, condition_id, token_id, outcome, side, trade_price,
                    quote_price, size, lifecycle_state, fill_reason,
                    maker_fee_equivalent_usdc, maker_rebate_conservative_usdc,
                    maker_rebate_base_usdc, maker_rebate_optimistic_usdc,
                    exchange_ts_ms, local_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill["run_id"],
                    fill["quote_uid"],
                    fill["condition_id"],
                    fill["token_id"],
                    fill["outcome"],
                    fill["side"],
                    fill["trade_price"],
                    fill["quote_price"],
                    fill["size"],
                    fill.get("lifecycle_state", "open"),
                    fill.get("fill_reason", ""),
                    fill.get("maker_fee_equivalent_usdc", 0.0),
                    fill.get("maker_rebate_conservative_usdc", 0.0),
                    fill.get("maker_rebate_base_usdc", 0.0),
                    fill.get("maker_rebate_optimistic_usdc", 0.0),
                    fill.get("exchange_ts_ms"),
                    fill["local_ts"],
                ),
            )

    def upsert_reward_inventory(self, inventory: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO reward_inventory (
                    run_id, condition_id, token_id, outcome, inventory_uid, state,
                    shares, avg_price, mark_price, unrealized_pnl, liquidation_pnl,
                    reward_credit_usdc, best_hedge_price, best_hedge_bundle_cost,
                    best_hedge_pnl, passive_exit_quote_uid, passive_exit_price,
                    opened_at, closed_at, close_mode, closed_pnl, reward_adjusted_pnl,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, condition_id, token_id) DO UPDATE SET
                    inventory_uid=excluded.inventory_uid,
                    state=excluded.state,
                    shares=excluded.shares,
                    avg_price=excluded.avg_price,
                    mark_price=excluded.mark_price,
                    unrealized_pnl=excluded.unrealized_pnl,
                    liquidation_pnl=excluded.liquidation_pnl,
                    reward_credit_usdc=excluded.reward_credit_usdc,
                    best_hedge_price=excluded.best_hedge_price,
                    best_hedge_bundle_cost=excluded.best_hedge_bundle_cost,
                    best_hedge_pnl=excluded.best_hedge_pnl,
                    passive_exit_quote_uid=excluded.passive_exit_quote_uid,
                    passive_exit_price=excluded.passive_exit_price,
                    opened_at=excluded.opened_at,
                    closed_at=excluded.closed_at,
                    close_mode=excluded.close_mode,
                    closed_pnl=excluded.closed_pnl,
                    reward_adjusted_pnl=excluded.reward_adjusted_pnl,
                    updated_at=excluded.updated_at
                """,
                (
                    inventory["run_id"],
                    inventory["condition_id"],
                    inventory["token_id"],
                    inventory["outcome"],
                    inventory.get("inventory_uid"),
                    inventory.get("state", "open_inventory"),
                    inventory["shares"],
                    inventory["avg_price"],
                    inventory.get("mark_price"),
                    inventory.get("unrealized_pnl"),
                    inventory.get("liquidation_pnl"),
                    inventory.get("reward_credit_usdc", 0.0),
                    inventory.get("best_hedge_price"),
                    inventory.get("best_hedge_bundle_cost"),
                    inventory.get("best_hedge_pnl"),
                    inventory.get("passive_exit_quote_uid"),
                    inventory.get("passive_exit_price"),
                    inventory.get("opened_at", 0.0),
                    inventory.get("closed_at"),
                    inventory.get("close_mode"),
                    inventory.get("closed_pnl"),
                    inventory.get("reward_adjusted_pnl"),
                    inventory.get("updated_at", time.time()),
                ),
            )

    def insert_reward_inventory_mark(self, inventory: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO reward_inventory_marks (
                    run_id, inventory_uid, condition_id, token_id, outcome, state,
                    shares, avg_price, mark_price, liquidation_pnl, reward_credit_usdc,
                    best_hedge_price, best_hedge_bundle_cost, best_hedge_pnl,
                    passive_exit_price, local_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inventory["run_id"],
                    inventory["inventory_uid"],
                    inventory["condition_id"],
                    inventory["token_id"],
                    inventory["outcome"],
                    inventory.get("state", "open_inventory"),
                    inventory["shares"],
                    inventory["avg_price"],
                    inventory.get("mark_price"),
                    inventory.get("liquidation_pnl"),
                    inventory.get("reward_credit_usdc", 0.0),
                    inventory.get("best_hedge_price"),
                    inventory.get("best_hedge_bundle_cost"),
                    inventory.get("best_hedge_pnl"),
                    inventory.get("passive_exit_price"),
                    inventory.get("updated_at", time.time()),
                ),
            )

    def insert_reward_inventory_closure(self, closure: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO reward_inventory_closures (
                    run_id, inventory_uid, condition_id, token_id, outcome, close_mode,
                    shares, entry_price, exit_price, hedge_price, bundle_cost,
                    realized_pnl, reward_credit_usdc, reward_adjusted_pnl, risk_reason,
                    trigger_pnl, exit_pnl, liquidation_pnl_at_trigger, hedge_pnl_at_trigger,
                    planned_max_loss_usdc, stop_slippage_usdc, exit_route, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    closure["run_id"],
                    closure["inventory_uid"],
                    closure["condition_id"],
                    closure["token_id"],
                    closure["outcome"],
                    closure["close_mode"],
                    closure["shares"],
                    closure["entry_price"],
                    closure.get("exit_price"),
                    closure.get("hedge_price"),
                    closure.get("bundle_cost"),
                    closure["realized_pnl"],
                    closure.get("reward_credit_usdc", 0.0),
                    closure["reward_adjusted_pnl"],
                    closure.get("risk_reason", ""),
                    closure.get("trigger_pnl"),
                    closure.get("exit_pnl", closure["realized_pnl"]),
                    closure.get("liquidation_pnl_at_trigger"),
                    closure.get("hedge_pnl_at_trigger"),
                    closure.get("planned_max_loss_usdc"),
                    closure.get("stop_slippage_usdc", 0.0),
                    closure.get("exit_route", ""),
                    closure["closed_at"],
                ),
            )

    def set_reward_metric(self, run_id: int, key: str, value: float) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO reward_metrics (run_id, metric_key, metric_value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id, metric_key) DO UPDATE SET
                    metric_value=excluded.metric_value, updated_at=excluded.updated_at
                """,
                (run_id, key, value, time.time()),
            )

    def insert_reward_event(
        self,
        run_id: int,
        event_type: str,
        message: str,
        *,
        level: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO reward_events
                    (run_id, level, event_type, message, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, level, event_type, message, _json(payload or {}), time.time()),
            )

    def reward_dashboard_snapshot(
        self, run_id: int | None = None, limit: int = 30
    ) -> dict[str, Any]:
        run_id = run_id or self.latest_reward_run_id()
        if run_id is None:
            return {
                "run": None,
                "metrics": {},
                "markets": [],
                "quotes": [],
                "fills": [],
                "inventory": [],
                "matched_bundles": [],
                "state_errors": [],
                "closures": [],
                "events": [],
            }
        with self.connect() as db:
            run = db.execute("SELECT * FROM reward_runs WHERE id=?", (run_id,)).fetchone()
            metrics = {
                row["metric_key"]: row["metric_value"]
                for row in db.execute("SELECT * FROM reward_metrics WHERE run_id=?", (run_id,))
            }
            config = json.loads(run["config_json"]) if run and run["config_json"] else {}
            metrics["max_loss_usdc"] = config.get("max_loss_usdc", 0.0)
            metrics["max_position_shares"] = config.get("max_position_shares", 0.0)
            metrics["max_exposure_usdc"] = config.get("max_exposure_usdc", 0.0)
            aggregate = db.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM reward_run_markets WHERE run_id=:run_id) AS markets_watched,
                  (SELECT COUNT(*) FROM reward_quotes
                    WHERE run_id=:run_id AND status IN ('open','cancel_requested'))
                    AS open_quotes,
                  (SELECT COUNT(*) FROM reward_quotes q
                   JOIN reward_run_markets rm
                     ON rm.run_id=q.run_id AND rm.condition_id=q.condition_id
                   WHERE q.run_id=:run_id AND q.status IN ('open','cancel_requested'))
                    AS selected_open_quotes,
                  (SELECT COUNT(*) FROM reward_quotes q
                   WHERE q.run_id=:run_id AND q.status IN ('open','cancel_requested')
                     AND NOT EXISTS (
                       SELECT 1 FROM reward_run_markets rm
                       WHERE rm.run_id=q.run_id AND rm.condition_id=q.condition_id
                     )
                     AND EXISTS (
                       SELECT 1 FROM reward_inventory i
                       WHERE i.run_id=q.run_id AND i.condition_id=q.condition_id
                         AND i.shares > 0 AND i.closed_at IS NULL
                     )) AS risk_only_quotes,
                  (SELECT COUNT(*) FROM reward_quotes q
                   WHERE q.run_id=:run_id AND q.status IN ('open','cancel_requested')
                     AND NOT EXISTS (
                       SELECT 1 FROM reward_run_markets rm
                       WHERE rm.run_id=q.run_id AND rm.condition_id=q.condition_id
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM reward_inventory i
                       WHERE i.run_id=q.run_id AND i.condition_id=q.condition_id
                         AND i.shares > 0 AND i.closed_at IS NULL
                     )) AS stale_open_quotes,
                  (SELECT COUNT(*) FROM reward_quotes WHERE run_id=:run_id AND locally_scoring=1
                    AND status='open') AS scoring_quotes,
                  (SELECT COUNT(*) FROM reward_quotes WHERE run_id=:run_id AND locally_scoring=0
                    AND status='open') AS non_scoring_quotes,
                  (SELECT COUNT(*) FROM reward_quotes
                    WHERE run_id=:run_id AND status='cancel_requested') AS cancel_pending_quotes,
                  (SELECT COUNT(*) FROM reward_quotes
                    WHERE run_id=:run_id AND status IN ('cancelled','replaced'))
                    AS cancel_acknowledged_quotes,
                  (SELECT COUNT(*) FROM reward_fills WHERE run_id=:run_id) AS would_have_fills,
                  (SELECT COUNT(*) FROM reward_fills
                    WHERE run_id=:run_id AND lifecycle_state='fill_before_cancel')
                    AS fill_before_cancel,
                  COALESCE((SELECT SUM(maker_fee_equivalent_usdc) FROM reward_fills
                    WHERE run_id=:run_id),0) AS maker_fee_equivalent_usdc,
                  COALESCE((SELECT SUM(maker_rebate_conservative_usdc) FROM reward_fills
                    WHERE run_id=:run_id),0) AS maker_rebate_conservative_usdc,
                  COALESCE((SELECT SUM(maker_rebate_base_usdc) FROM reward_fills
                    WHERE run_id=:run_id),0) AS maker_rebate_base_usdc,
                  COALESCE((SELECT SUM(maker_rebate_optimistic_usdc) FROM reward_fills
                    WHERE run_id=:run_id),0) AS maker_rebate_optimistic_usdc,
                  COALESCE((SELECT SUM(shares * avg_price) FROM reward_inventory
                    WHERE run_id=:run_id AND closed_at IS NULL),0) AS inventory_exposure,
                  COALESCE((SELECT SUM(unrealized_pnl) FROM reward_inventory
                    WHERE run_id=:run_id AND closed_at IS NULL),0) AS inventory_unrealized_pnl,
                  (SELECT COUNT(*) FROM reward_inventory_closures
                    WHERE run_id=:run_id AND close_mode='hedge_locked') AS locked_bundles,
                  COALESCE((SELECT SUM(realized_pnl) FROM reward_inventory_closures
                    WHERE run_id=:run_id AND close_mode='hedge_locked'),0) AS locked_bundle_pnl,
                  (SELECT COUNT(*) FROM reward_inventory_closures
                    WHERE run_id=:run_id AND close_mode='passive_exit') AS passive_exits,
                  COALESCE((SELECT SUM(realized_pnl) FROM reward_inventory_closures
                    WHERE run_id=:run_id AND close_mode='passive_exit'),0) AS passive_exit_pnl,
                  (SELECT COUNT(*) FROM reward_inventory_closures
                    WHERE run_id=:run_id AND close_mode='emergency_exit') AS emergency_exits,
                  COALESCE((SELECT SUM(realized_pnl) FROM reward_inventory_closures
                    WHERE run_id=:run_id AND close_mode='emergency_exit'),0) AS emergency_exit_pnl,
                  COALESCE((SELECT SUM(stop_slippage_usdc) FROM reward_inventory_closures
                    WHERE run_id=:run_id),0) AS stop_slippage_usdc,
                  COALESCE((SELECT SUM(reward_adjusted_pnl) FROM reward_inventory_closures
                    WHERE run_id=:run_id),0) AS closed_reward_adjusted_pnl
                """,
                {"run_id": run_id},
            ).fetchone()
            metrics.update(dict(aggregate))
            markets = _rows(
                db.execute(
                    """
                    SELECT m.*,
                      (SELECT COUNT(*) FROM reward_quotes q
                       WHERE q.run_id=? AND q.condition_id=m.condition_id
                         AND q.status IN ('open','cancel_requested')) AS open_quotes,
                      (SELECT COUNT(*) FROM reward_quotes q
                       WHERE q.run_id=? AND q.condition_id=m.condition_id
                         AND q.status='open' AND q.locally_scoring=1) AS scoring_quotes
                    FROM reward_run_markets rm JOIN reward_markets m
                      ON m.condition_id=rm.condition_id
                    WHERE rm.run_id=?
                    ORDER BY m.rewards_rate_per_day DESC, m.volume_24hr DESC
                    LIMIT ?
                    """,
                    (run_id, run_id, run_id, limit),
                ).fetchall()
            )
            quotes = _rows(
                db.execute(
                    """
                    SELECT q.*, s.midpoint, s.best_bid, s.best_ask, s.spread,
                      s.age_seconds, s.within_spread, s.estimated_reward_usdc,
                      m.question, m.rewards_rate_per_day, m.rewards_min_size,
                      m.minimum_order_age_seconds
                    FROM reward_quotes q
                    JOIN reward_markets m ON m.condition_id=q.condition_id
                    LEFT JOIN reward_quote_snapshots s ON s.id=(
                      SELECT id FROM reward_quote_snapshots latest
                      WHERE latest.run_id=q.run_id AND latest.quote_uid=q.quote_uid
                      ORDER BY latest.local_ts DESC LIMIT 1
                    )
                    WHERE q.run_id=? AND q.status IN ('open','cancel_requested')
                      AND (
                        EXISTS (
                          SELECT 1 FROM reward_run_markets rm
                          WHERE rm.run_id=q.run_id AND rm.condition_id=q.condition_id
                        )
                        OR EXISTS (
                          SELECT 1 FROM reward_inventory i
                          WHERE i.run_id=q.run_id AND i.condition_id=q.condition_id
                            AND i.shares > 0 AND i.closed_at IS NULL
                        )
                      )
                    ORDER BY q.locally_scoring DESC, m.rewards_rate_per_day DESC,
                      q.opened_at DESC
                    LIMIT ?
                    """,
                    (run_id, limit),
                ).fetchall()
            )
            fills = _rows(
                db.execute(
                    """
                    SELECT f.*, m.question FROM reward_fills f
                    JOIN reward_markets m ON m.condition_id=f.condition_id
                    WHERE f.run_id=? ORDER BY f.local_ts DESC LIMIT ?
                    """,
                    (run_id, limit),
                ).fetchall()
            )
            inventory = _rows(
                db.execute(
                    """
                    SELECT i.*, m.question FROM reward_inventory i
                    JOIN reward_markets m ON m.condition_id=i.condition_id
                    WHERE i.run_id=? AND i.shares > 0 AND i.closed_at IS NULL
                    ORDER BY ABS(COALESCE(i.unrealized_pnl,0)) DESC LIMIT ?
                    """,
                    (run_id, limit),
                ).fetchall()
            )
            state_errors = _rows(
                db.execute(
                    """
                    SELECT i.*, m.question FROM reward_inventory i
                    JOIN reward_markets m ON m.condition_id=i.condition_id
                    WHERE i.run_id=? AND i.shares > 0 AND i.closed_at IS NOT NULL
                    ORDER BY ABS(COALESCE(i.unrealized_pnl,0)) DESC LIMIT ?
                    """,
                    (run_id, limit),
                ).fetchall()
            )
            matched_bundles = _reward_matched_bundles(inventory)
            _annotate_reward_inventory_risk(inventory, metrics["max_loss_usdc"])
            metrics["matched_bundle_positions"] = len(matched_bundles)
            metrics["matched_bundle_shares"] = sum(
                row["matched_shares"] for row in matched_bundles
            )
            metrics["matched_bundle_pnl"] = sum(row["matched_pnl"] for row in matched_bundles)
            metrics["unhedged_shares"] = sum(row.get("unhedged_shares", 0.0) for row in inventory)
            metrics["unhedged_pnl"] = sum(row.get("unhedged_pnl", 0.0) for row in inventory)
            metrics["inventory_state_errors"] = len(state_errors)
            closures = _rows(
                db.execute(
                    """
                    SELECT c.*, m.question FROM reward_inventory_closures c
                    JOIN reward_markets m ON m.condition_id=c.condition_id
                    WHERE c.run_id=? ORDER BY c.closed_at DESC LIMIT ?
                    """,
                    (run_id, limit),
                ).fetchall()
            )
            events = _rows(
                db.execute(
                    "SELECT * FROM reward_events WHERE run_id=? ORDER BY created_at DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            )
        return {
            "run": dict(run) if run else None,
            "metrics": metrics,
            "markets": markets,
            "quotes": quotes,
            "fills": fills,
            "inventory": inventory,
            "matched_bundles": matched_bundles,
            "state_errors": state_errors,
            "closures": closures,
            "events": events,
        }


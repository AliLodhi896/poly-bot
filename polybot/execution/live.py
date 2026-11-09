from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from polybot.api.geoblock import GeoblockClient, GeoblockResult
from polybot.config import Settings
from polybot.risk import RiskEngine

# py_clob_client_v2 logs every transient httpx connection drop / read timeout at ERROR
# with this exact format string. The reward live loop already catches the resulting
# exceptions and records reward_events rows, so on the operator's terminal these are
# benign duplicates that bypass polybot's own logging (they reach logging's lastResort
# stderr handler because polybot configures no handlers of its own).
_V2_TRANSIENT_REQUEST_ERROR = "[py_clob_client_v2] request error: %s"
# The originating logger (helpers.py does ``logging.getLogger(__name__)``). The filter
# must sit on this logger, not a parent: a logger's own filters are the only ones
# consulted before a record is dispatched to handlers, so a filter on the parent
# ``py_clob_client_v2`` logger would not see records emitted by this child.
_V2_REQUEST_LOGGER = "py_clob_client_v2.http_helpers.helpers"


class _TransientRequestErrorFilter(logging.Filter):
    """Drop py_clob_client_v2's transient connection/timeout error lines.

    Matched on the exact format string so genuine API errors
    (``request error status=... url=... body=...``) and the retry notice stay visible.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 — logging API name
        return record.msg != _V2_TRANSIENT_REQUEST_ERROR


_transient_request_error_filter: _TransientRequestErrorFilter | None = None


def quiet_v2_transient_request_errors(enabled: bool = True) -> None:
    """Idempotently install a filter that hides v2's transient request-error noise.

    No-op when ``enabled`` is false or the filter is already installed. Leaves all
    other py_clob_client_v2 logging — including real API status/body errors — intact.
    """
    global _transient_request_error_filter
    if not enabled or _transient_request_error_filter is not None:
        return
    _transient_request_error_filter = _TransientRequestErrorFilter()
    logging.getLogger(_V2_REQUEST_LOGGER).addFilter(_transient_request_error_filter)


class LiveExecutionError(RuntimeError):
    pass


class LiveTradingDisabled(LiveExecutionError):
    pass


class Geoblocked(LiveExecutionError):
    pass


class MissingCredentials(LiveExecutionError):
    pass


class MissingLiveDependency(LiveExecutionError):
    pass


class UnsafeOrder(LiveExecutionError):
    pass


class LiveExecutor:
    """Hard-gated live executor.

    The MVP exposes only guarded integration points. It never asks for secrets and
    never places an order unless ``POLYBOT_LIVE_ENABLED=true`` and all runtime
    checks pass.
    """

    def __init__(
        self,
        settings: Settings,
        geoblock_check: Callable[[], Awaitable[GeoblockResult]] | None = None,
    ) -> None:
        self.settings = settings
        self.risk = RiskEngine(settings)
        self._geoblock_check = geoblock_check

    def validate_maker_order(
        self,
        order_type: Literal["GTC", "GTD", "FOK", "FAK"],
        post_only: bool,
    ) -> None:
        if order_type not in {"GTC", "GTD"}:
            raise UnsafeOrder("maker orders must be GTC or GTD")
        if not post_only:
            raise UnsafeOrder("maker orders must be post-only")

    def validate_arb_order(
        self,
        order_type: Literal["GTC", "GTD", "FOK", "FAK"],
    ) -> None:
        if order_type not in {"FOK", "FAK"}:
            raise UnsafeOrder("arb executor only supports FOK or FAK in live mode")

    async def ensure_live_allowed(self) -> GeoblockResult:
        if not self.settings.live_enabled:
            raise LiveTradingDisabled("live trading disabled; set POLYBOT_LIVE_ENABLED=true")
        if self.risk.kill_switch_active():
            raise LiveTradingDisabled("kill switch active")
        result = await self._check_geoblock()
        if result.blocked:
            location = "/".join(part for part in [result.country, result.region] if part)
            raise Geoblocked(f"Polymarket trading geoblocked for detected location {location}")
        missing = self._missing_credentials()
        if missing:
            raise MissingCredentials(f"missing live credentials: {', '.join(missing)}")
        return result

    async def _check_geoblock(self) -> GeoblockResult:
        if self._geoblock_check is not None:
            return await self._geoblock_check()
        async with GeoblockClient(self.settings) as client:
            return await client.check()

    def _missing_credentials(self) -> list[str]:
        required = {
            "POLYBOT_WALLET_PRIVATE_KEY": self.settings.wallet_private_key,
            "POLYBOT_WALLET_FUNDER_ADDRESS": self.settings.wallet_funder_address,
        }
        return [name for name, value in required.items() if not value]

    def _build_py_clob_client(self) -> tuple[Any, Any]:
        """Build a py-clob-client-v2 client for the deposit-wallet flow.

        Returns the client plus the ``py_clob_client_v2`` module so callers can
        reach its order/type classes (OrderArgs, OrderType, Side, ...). The old
        py-clob-client is archived; v2 supports signature_type 3 (POLY_1271).
        """
        try:
            import py_clob_client_v2 as v2
        except ImportError as exc:
            raise MissingLiveDependency(
                "py-clob-client-v2 is not installed — run `uv sync --extra live` "
                "(plus any other extras you use, e.g. `--extra dev`), then restart the bot"
            ) from exc

        quiet_v2_transient_request_errors(self.settings.quiet_clob_v2_request_errors)

        client = v2.ClobClient(
            self.settings.clob_host,
            chain_id=self.settings.chain_id,
            key=self.settings.wallet_private_key,
            signature_type=self.settings.wallet_signature_type,
            funder=self.settings.wallet_funder_address,
        )
        # Derive first (the key usually already exists from the dry-run, so this avoids a noisy
        # 400 "Could not create api key"); fall back to create-or-derive for a fresh wallet.
        try:
            creds = client.derive_api_key()
        except Exception:  # noqa: BLE001 — fall back to the create path on a fresh wallet
            creds = client.create_or_derive_api_key()
        client.set_api_creds(creds)
        return client, v2, creds

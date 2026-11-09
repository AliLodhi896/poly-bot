from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from polybot.api.geoblock import GeoblockResult
from polybot.config import Settings
from polybot.execution.live import LiveExecutor, MissingCredentials


@dataclass(frozen=True)
class PositionInfo:
    """A real on-chain outcome-token position from the Polymarket data-api."""

    token_id: str
    size: float
    avg_price: float
    cur_price: float
    condition_id: str
    outcome: str
    redeemable: bool


def _parse_usdc(response: Any) -> float:
    """Best-effort parse of a get_balance_allowance response into pUSD/USDC dollars."""
    raw: Any = response
    if isinstance(response, dict):
        raw = response.get("balance", response.get("balance_allowance", response))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    # CLOB balances are returned in 6-decimal base units; scale down when clearly raw.
    return value / 1_000_000 if value > 100_000 else value


def _extract_order_id(response: Any) -> str:
    if isinstance(response, dict):
        for key in ("orderID", "orderId", "order_id", "orderHash", "id"):
            if response.get(key):
                return str(response[key])
    return str(response)


def _extract_batch_order_ids(response: Any, count: int) -> list[str | None]:
    """Map a batch ``post_orders`` response to one order id per spec (None on failure).

    The CLOB returns a list of per-order dicts in the SAME order as the submitted orders:
    ``[{"success": true, "orderID": "0x..", "status": "live", "errorMsg": ""}, ...]`` (verified
    via scripts/batch_probe.py). If the shape or length is unexpected, return all None so the
    caller tracks nothing and the orphan reaper cleans up — never a mistracked/duplicated order.
    """
    if isinstance(response, list) and len(response) == count:
        ids: list[str | None] = []
        for item in response:
            if isinstance(item, dict) and item.get("success"):
                order_id = None
                for key in ("orderID", "orderId", "order_id", "orderHash", "id"):
                    if item.get(key):
                        order_id = str(item[key])
                        break
                ids.append(order_id)
            else:
                ids.append(None)
        return ids
    return [None] * count


def _parse_canceled(response: Any, requested: list[str]) -> set[str]:
    """Which of ``requested`` order ids the batch-cancel response confirms canceled.

    Conservative: only treat ids as canceled when the response clearly says so, so a still-
    resting order is never dropped from tracking (which would let a replacement double up the
    collateral). The CLOB returns ``{"canceled": [...], "not_canceled": {id: reason}}``.
    Unknown response shapes confirm nothing.
    """
    req = {str(o) for o in requested}
    if isinstance(response, dict):
        canceled = response.get("canceled", response.get("cancelled"))
        if isinstance(canceled, list):
            return {str(x) for x in canceled} & req
        if isinstance(canceled, str):
            return {canceled} & req
        not_canceled = response.get("not_canceled")
        if isinstance(not_canceled, dict):
            return req - {str(k) for k in not_canceled}
    if isinstance(response, list):
        out: set[str] = set()
        for item in response:
            if not isinstance(item, dict):
                continue
            order_id = None
            for key in ("orderID", "orderId", "order_id", "orderHash", "id"):
                if item.get(key):
                    order_id = str(item[key])
                    break
            if order_id not in req:
                continue
            status = str(item.get("status") or "").lower()
            if item.get("success") is True or status in {"canceled", "cancelled"}:
                out.add(order_id)
        return out
    return set()


class LiveOrderGateway:
    """The single place that performs real CLOB order calls (py-clob-client-v2).

    Every method is hard-gated through :class:`LiveExecutor`: ``start`` enforces
    ``POLYBOT_LIVE_ENABLED``, kill switch, geoblock and credential checks before
    any order can be placed. The v2 SDK and its order/type classes are loaded
    lazily (via ``LiveExecutor._build_py_clob_client``) so the module imports in
    paper-only environments; tests inject a fake client and a fake ``v2`` namespace.

    Deposit-wallet accounts use ``signature_type=3`` (POLY_1271) with the deposit
    wallet as ``funder`` — configured via settings, applied in the client build.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        v2: Any = None,
        geoblock_check: Callable[[], Awaitable[GeoblockResult]] | None = None,
    ) -> None:
        self.settings = settings
        self.executor = LiveExecutor(settings, geoblock_check=geoblock_check)
        self._client = client
        self._v2 = v2
        self._creds: Any = None
        self._started = client is not None

    async def start(self) -> GeoblockResult:
        result = await self.executor.ensure_live_allowed()
        if self._client is None:
            self._client, self._v2, self._creds = self.executor._build_py_clob_client()
        self._started = True
        return result

    def api_creds(self) -> tuple[str, str, str]:
        """Return (api_key, secret, passphrase) for the authenticated user channel."""
        creds = self._creds
        if creds is None:
            raise MissingCredentials("api creds unavailable; start() not run against a live client")
        return (creds.api_key, creds.api_secret, creds.api_passphrase)

    def _ensure_started(self) -> None:
        if not self._started or self._client is None or self._v2 is None:
            raise MissingCredentials("gateway not started; call start() first")

    def usdc_balance(self) -> float:
        self._ensure_started()
        params = self._v2.BalanceAllowanceParams(
            asset_type=self._v2.AssetType.COLLATERAL,
            signature_type=self.settings.wallet_signature_type,
        )
        return _parse_usdc(self._client.get_balance_allowance(params))

    def place_post_only(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        tick_size: float,
        neg_risk: bool = False,
    ) -> str:
        self._ensure_started()
        self.executor.validate_maker_order("GTC", True)
        order_args = self._v2.OrderArgs(
            token_id=token_id,
            price=float(price),
            size=float(size),
            side=getattr(self._v2.Side, side.upper()),
        )
        options = self._v2.PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=neg_risk)
        response = self._client.create_and_post_order(
            order_args=order_args,
            options=options,
            order_type=self._v2.OrderType.GTC,
            post_only=True,
        )
        return _extract_order_id(response)

    def post_orders_raw(
        self, specs: list[tuple[str, str, float, float, float, bool]]
    ) -> Any:
        """Place several post-only GTC orders in ONE batch; return the RAW CLOB response.

        Each spec is ``(token_id, side, price, size, tick_size, neg_risk)``. Orders are built
        and signed exactly like :meth:`place_post_only` (just batched via ``post_orders``). The
        response is returned unparsed so the batch-response shape can be verified before batch
        placement is wired into the live reconcile (its format is undocumented).
        """
        self._ensure_started()
        self.executor.validate_maker_order("GTC", True)
        args = []
        for token_id, side, price, size, tick_size, neg_risk in specs:
            order_args = self._v2.OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=getattr(self._v2.Side, side.upper()),
            )
            options = self._v2.PartialCreateOrderOptions(
                tick_size=str(tick_size), neg_risk=neg_risk
            )
            signed = self._client.create_order(order_args=order_args, options=options)
            args.append(self._v2.PostOrdersV2Args(order=signed, orderType=self._v2.OrderType.GTC))
        return self._client.post_orders(args, post_only=True)

    def cancel_orders_raw(self, order_ids: list[str]) -> Any:
        """Cancel orders and return the RAW CLOB response (unparsed).

        Used by the probe to verify the batch-cancel response shape (whether a confirmed
        cancel that still shows in ``open_orders`` is real failure or read lag).
        """
        self._ensure_started()
        return self._client.cancel_orders(list(order_ids))

    def place_many(
        self, specs: list[tuple[str, str, float, float, float, bool]]
    ) -> list[str | None]:
        """Place several post-only GTC orders in ONE batch; return an order id per spec.

        ``None`` for any order the CLOB rejected (or that can't be matched back). Collapses N
        sequential ``POST /order`` round-trips into one, so a multi-market reconcile doesn't
        freeze the event loop. The order ids align with ``specs`` by position.
        """
        if not specs:
            return []
        response = self.post_orders_raw(specs)
        return _extract_batch_order_ids(response, len(specs))

    def open_orders(self) -> list[tuple[str, str]]:
        """Return ``(order_id, token_id)`` for every resting order on the account.

        Used by the live coordinator's orphan sweep to detect resting orders it is
        no longer tracking (e.g. a cancel the CLOB never processed). The account is
        treated as exclusively the bot's, consistent with the ``cancel_all`` issued
        at run start/stop.
        """
        self._ensure_started()
        orders = self._client.get_open_orders()
        result: list[tuple[str, str]] = []
        for order in orders or []:
            if not isinstance(order, dict):
                continue
            order_id = order.get("id") or order.get("orderID") or order.get("order_id")
            if not order_id:
                continue
            token = order.get("asset_id") or order.get("token_id") or order.get("market") or ""
            result.append((str(order_id), str(token)))
        return result

    def positions(self) -> list[PositionInfo]:
        """Real on-chain outcome-token positions for the wallet (Polymarket data-api).

        Read-only and keyed by the public funder/proxy address — no signing. Used by the
        inventory↔wallet reconciliation to adopt positions the bot under-tracks. Returns
        only non-empty holdings; the caller filters dust/resolved markets.
        """
        addr = self.settings.wallet_funder_address
        if not addr:
            return []
        resp = httpx.get(
            "https://data-api.polymarket.com/positions",
            params={"user": addr},
            timeout=self.settings.request_timeout_seconds,
        )
        resp.raise_for_status()
        out: list[PositionInfo] = []
        for p in resp.json() or []:
            if not isinstance(p, dict):
                continue
            token = str(p.get("asset") or "")
            size = float(p.get("size") or 0.0)
            if not token or size <= 1e-9:
                continue
            out.append(
                PositionInfo(
                    token_id=token,
                    size=size,
                    avg_price=float(p.get("avgPrice") or 0.0),
                    cur_price=float(p.get("curPrice") or 0.0),
                    condition_id=str(p.get("conditionId") or ""),
                    outcome=str(p.get("outcome") or ""),
                    redeemable=bool(p.get("redeemable", False)),
                )
            )
        return out

    def cancel(self, order_id: str) -> bool:
        self._ensure_started()
        response = self._client.cancel_orders([order_id])
        return order_id in _parse_canceled(response, [order_id])

    def cancel_many(self, order_ids: list[str]) -> set[str]:
        """Batch-cancel orders in ONE request; return the ids the CLOB confirms canceled.

        Collapses N sequential ``DELETE /order`` round-trips into one ``DELETE /orders`` so a
        multi-market reconcile doesn't freeze the event loop. Same conservative tracking
        semantics as :meth:`cancel` (the caller drops only confirmed-canceled ids).
        """
        if not order_ids:
            return set()
        self._ensure_started()
        response = self._client.cancel_orders(list(order_ids))
        return _parse_canceled(response, order_ids)

    def cancel_all(self) -> Any:
        self._ensure_started()
        return self._client.cancel_all()

    def take_fok(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        tick_size: float,
        neg_risk: bool = False,
    ) -> Any:
        """Marketable, price-capped FOK order (used only to lock a profitable bundle).

        A fill-or-kill limit at ``price`` for ``size`` shares: it fills fully at a price
        no worse than the cap or is killed — never a slippery open-ended market order.
        """
        self._ensure_started()
        self.executor.validate_arb_order("FOK")
        order_args = self._v2.OrderArgs(
            token_id=token_id,
            price=float(price),
            size=float(size),
            side=getattr(self._v2.Side, side.upper()),
        )
        options = self._v2.PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=neg_risk)
        return self._client.create_and_post_order(
            order_args=order_args,
            options=options,
            order_type=self._v2.OrderType.FOK,
            post_only=False,
        )

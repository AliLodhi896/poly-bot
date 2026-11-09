from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import certifi
import websockets

from polybot.config import Settings


@dataclass(frozen=True)
class MakerFill:
    """One of OUR resting (maker) orders matched inside a user-channel ``trade`` event.

    Polymarket reports a maker fill from the *taker's* perspective at the top level
    (``side``/``size`` describe the taker, who may have swept several makers), while our
    actual fill lives in ``maker_orders[]`` keyed by ``order_id``. We therefore read the
    per-maker ``matched_amount``/``price``/``asset_id`` here and leave the *side* of the
    fill to the caller, which knows it from the resting order it placed.
    """

    trade_id: str
    order_id: str
    token_id: str
    price: float
    size: float
    status: str
    taker_side: str


def parse_maker_fills(payload: dict[str, Any]) -> list[MakerFill]:
    """Extract every maker-order match from a user-channel ``trade`` event.

    Returns one :class:`MakerFill` per entry in ``maker_orders[]`` that carries an order id,
    asset id, price and matched amount. The caller filters these down to the order ids it
    actually placed and supplies the fill side. The same trade is delivered repeatedly as its
    status advances (``MATCHED`` -> ``MINED`` -> ``CONFIRMED``), so the caller must dedupe by
    ``(trade_id, order_id)``.
    """
    if not isinstance(payload, dict):
        return []
    event_type = str(payload.get("event_type") or payload.get("type") or "").lower()
    if event_type != "trade":
        return []
    makers = payload.get("maker_orders")
    if not isinstance(makers, list):
        return []
    trade_id = str(payload.get("id") or payload.get("trade_id") or "")
    status = str(payload.get("status") or "").upper()
    taker_side = str(payload.get("side") or "").upper()
    fills: list[MakerFill] = []
    for maker in makers:
        if not isinstance(maker, dict):
            continue
        order_id = str(
            maker.get("order_id") or maker.get("orderID") or maker.get("id") or ""
        )
        token_id = str(
            maker.get("asset_id") or maker.get("token_id") or maker.get("assetId") or ""
        )
        price = maker.get("price")
        size = (
            maker.get("matched_amount")
            or maker.get("matchedAmount")
            or maker.get("size")
        )
        if not order_id or not token_id or price is None or size is None:
            continue
        try:
            price_f = float(price)
            size_f = float(size)
        except (TypeError, ValueError):
            continue
        if size_f <= 0:
            continue
        fills.append(
            MakerFill(
                trade_id=trade_id,
                order_id=order_id,
                token_id=token_id,
                price=price_f,
                size=size_f,
                status=status,
                taker_side=taker_side,
            )
        )
    return fills


class UserWebSocket:
    """Authenticated Polymarket user channel: real fills/order status for our orders."""

    def __init__(
        self, settings: Settings, api_key: str, secret: str, passphrase: str
    ) -> None:
        self.settings = settings
        self._auth = {"apiKey": api_key, "secret": secret, "passphrase": passphrase}

    async def stream(self, markets: list[str]) -> AsyncIterator[dict[str, Any]]:
        subscription = {"auth": self._auth, "markets": markets, "type": "user"}
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        while True:
            try:
                async with websockets.connect(
                    self.settings.user_ws_url,
                    ping_interval=10,
                    ping_timeout=10,
                    close_timeout=2,
                    max_queue=2048,
                    ssl=ssl_context,
                ) as websocket:
                    await websocket.send(json.dumps(subscription))
                    yield {"event_type": "__connected__"}
                    async for message in websocket:
                        if isinstance(message, bytes):
                            message = message.decode("utf-8")
                        if not message or message in {"PONG", "pong"}:
                            continue
                        try:
                            payload = json.loads(message)
                        except json.JSONDecodeError:
                            continue
                        items = payload if isinstance(payload, list) else [payload]
                        for item in items:
                            if isinstance(item, dict):
                                yield item
            except (OSError, websockets.WebSocketException) as exc:
                yield {"event_type": "__error__", "message": str(exc)}
                await asyncio.sleep(1)

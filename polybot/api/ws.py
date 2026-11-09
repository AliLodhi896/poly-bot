from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import websockets

from polybot.config import Settings


class MarketWebSocket:
    """Public Polymarket market-data WebSocket helper."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def stream(
        self,
        asset_ids: list[str],
        custom_feature_enabled: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        subscription = {
            "assets_ids": asset_ids,
            "type": "market",
            "custom_feature_enabled": custom_feature_enabled,
        }
        async with websockets.connect(self.settings.market_ws_url) as websocket:
            await websocket.send(json.dumps(subscription))
            heartbeat = asyncio.create_task(self._heartbeat(websocket))
            try:
                async for message in websocket:
                    if isinstance(message, bytes):
                        message = message.decode("utf-8")
                    if message == "PONG":
                        continue
                    try:
                        payload = json.loads(message)
                    except json.JSONDecodeError:
                        payload = {"event_type": "raw", "message": message}
                    yield payload
            finally:
                heartbeat.cancel()

    async def _heartbeat(self, websocket: Any) -> None:
        while True:
            await asyncio.sleep(10)
            await websocket.send("PING")

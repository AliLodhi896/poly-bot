from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from typing import Any, TypeVar

import certifi
import websockets

from polybot.config import Settings

T = TypeVar("T")


def chunked(items: list[T], size: int) -> list[list[T]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


class MarketWebSocket:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def stream(self, asset_ids: list[str]) -> AsyncIterator[dict[str, Any]]:
        subscription = {
            "assets_ids": asset_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        while True:
            try:
                async with websockets.connect(
                    self.settings.market_ws_url,
                    ping_interval=10,
                    ping_timeout=10,
                    close_timeout=2,
                    max_queue=2048,
                    ssl=ssl_context,
                ) as websocket:
                    await websocket.send(json.dumps(subscription))
                    yield {"event_type": "__connected__", "assets_count": len(asset_ids)}
                    async for message in websocket:
                        if isinstance(message, bytes):
                            message = message.decode("utf-8")
                        if not message or message in {"PONG", "pong"}:
                            continue
                        try:
                            payload = json.loads(message)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(payload, list):
                            for item in payload:
                                if isinstance(item, dict):
                                    yield item
                        elif isinstance(payload, dict):
                            yield payload
            except (OSError, websockets.WebSocketException) as exc:
                yield {
                    "event_type": "__error__",
                    "message": str(exc),
                    "error_type": type(exc).__name__,
                }
                await asyncio.sleep(1)

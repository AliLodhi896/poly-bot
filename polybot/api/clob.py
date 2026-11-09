from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from itertools import islice
from typing import Any, TypeVar

import httpx

from polybot.config import Settings
from polybot.models import Book, Market

T = TypeVar("T")


@dataclass(frozen=True)
class MarketFeeInfo:
    rate: float = 0.0
    enabled: bool = False
    taker_only: bool = True


@dataclass(frozen=True)
class ClobMarketInfo:
    min_order_size: float = 1.0
    tick_size: Decimal = Decimal("0.01")
    minimum_order_age_seconds: int = 0
    fee_info: MarketFeeInfo = MarketFeeInfo()
    neg_risk: bool = False
    raw: dict[str, Any] | None = None


def batched(items: list[T], size: int) -> list[list[T]]:
    iterator = iter(items)
    batches: list[list[T]] = []
    while batch := list(islice(iterator, size)):
        batches.append(batch)
    return batches


class ClobClient:
    """Read-only CLOB client.

    Trading endpoints are intentionally excluded from this class; live paths live
    behind hard safety gates in ``polybot.execution.live``.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=settings.clob_host,
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
        )

    async def __aenter__(self) -> ClobClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._own_client:
            await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.settings.max_retries + 1):
            try:
                response = await self.client.request(method, path, **kwargs)
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                retryable = isinstance(exc, httpx.RequestError) or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and (exc.response.status_code == 429 or exc.response.status_code >= 500)
                )
                if not retryable or attempt >= self.settings.max_retries:
                    raise
                await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
        raise RuntimeError("unreachable request retry state")

    async def health(self) -> bool:
        response = await self._request("GET", "/")
        return response.status_code == 200

    async def server_time(self) -> int | None:
        response = await self._request("GET", "/time")
        payload = response.json()
        if isinstance(payload, dict):
            value = payload.get("time") or payload.get("serverTime")
        else:
            value = payload
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def get_order_book(self, token_id: str) -> Book:
        response = await self._request("GET", "/book", params={"token_id": token_id})
        return Book.from_api(response.json())

    async def get_order_books(self, token_ids: list[str], chunk_size: int = 100) -> dict[str, Book]:
        books: dict[str, Book] = {}
        for chunk in batched(token_ids, chunk_size):
            body = [{"token_id": token_id} for token_id in chunk]
            response = await self._request("POST", "/books", json=body)
            payload = response.json()
            if isinstance(payload, list):
                for raw_book in payload:
                    if isinstance(raw_book, dict):
                        book = Book.from_api(raw_book)
                        books[book.asset_id] = book
        return books

    async def price_volatility(
        self, token_id: str, interval: str = "6h", fidelity: int = 1
    ) -> tuple[float, float] | None:
        """Recent intraday volatility of a token from prices-history.

        Returns ``(range, max_step)`` over the window: ``range`` = max-min price (overall swing),
        ``max_step`` = the largest move between consecutive samples (a proxy for the fast jumps /
        sweeps that beat the bot's cancel latency and cause adverse fills). ``None`` if no data.
        """
        history = await self._prices_history(token_id, interval=interval, fidelity=fidelity)
        prices = [
            float(point["p"])
            for point in history
            if isinstance(point, dict) and point.get("p") is not None
        ]
        if len(prices) < 2:
            return None
        price_range = max(prices) - min(prices)
        max_step = max(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices)))
        return price_range, max_step

    async def price_history(
        self, token_id: str, interval: str = "6h", fidelity: int = 1
    ) -> list[tuple[float, float]]:
        """Timestamped price history as ``(unix_ts_seconds, price)`` points.

        The CLOB endpoint has used slightly different timestamp keys across clients; parse the
        common variants and return an empty list when history is unavailable.
        """
        history = await self._prices_history(token_id, interval=interval, fidelity=fidelity)
        points: list[tuple[float, float]] = []
        for point in history:
            if not isinstance(point, dict) or point.get("p") is None:
                continue
            ts = point.get("t") or point.get("timestamp") or point.get("time")
            if ts is None:
                continue
            try:
                timestamp = float(ts)
                price = float(point["p"])
            except (TypeError, ValueError):
                continue
            if timestamp > 10_000_000_000:
                timestamp /= 1000.0
            points.append((timestamp, price))
        points.sort(key=lambda item: item[0])
        return points

    async def _prices_history(
        self, token_id: str, interval: str = "6h", fidelity: int = 1
    ) -> list[object]:
        try:
            response = await self._request(
                "GET",
                "/prices-history",
                params={"market": token_id, "interval": interval, "fidelity": fidelity},
            )
        except httpx.HTTPError:
            return []
        payload = response.json()
        history = payload.get("history") if isinstance(payload, dict) else payload
        return list(history or []) if isinstance(history, list) else []

    async def get_market_fee_info(self, condition_id: str) -> MarketFeeInfo:
        return (await self.get_clob_market_info(condition_id)).fee_info

    async def get_clob_market_info(self, condition_id: str) -> ClobMarketInfo:
        response = await self._request("GET", f"/clob-markets/{condition_id}")
        payload = response.json()
        details = payload.get("fee_details") or payload.get("fd") or {}
        rate = float(details.get("rate") or details.get("r") or 0.0)
        taker_only = bool(details.get("taker_only", details.get("to", True)))
        enabled = bool(details.get("enabled", details.get("e", rate > 0))) and rate > 0
        tick = (
            payload.get("minimum_tick_size")
            or payload.get("min_tick_size")
            or payload.get("mts")
        )
        min_order = payload.get("minimum_order_size") or payload.get("min_order_size")
        if min_order is None:
            min_order = payload.get("mos")
        order_age = payload.get("minimum_order_age_seconds") or payload.get("oas") or 0
        neg_risk = bool(
            payload.get("neg_risk", payload.get("negRisk", payload.get("nr", False)))
        )
        return ClobMarketInfo(
            min_order_size=float(min_order or 1.0),
            tick_size=Decimal(str(tick or "0.01")),
            minimum_order_age_seconds=int(float(order_age or 0)),
            fee_info=MarketFeeInfo(
                rate=rate if enabled else 0.0,
                enabled=enabled,
                taker_only=taker_only,
            ),
            neg_risk=neg_risk,
            raw=payload if isinstance(payload, dict) else None,
        )

    async def get_rewards_markets(
        self,
        limit: int = 100,
        order_by: str = "rate_per_day",
        position: str = "DESC",
        next_cursor: str | None = None,
    ) -> tuple[list[Market], str | None]:
        params: dict[str, Any] = {
            "page_size": min(limit, 500),
            "order_by": order_by,
            "position": position,
        }
        if next_cursor:
            params["next_cursor"] = next_cursor
        response = await self._request("GET", "/rewards/markets/multi", params=params)
        payload = response.json()
        if not isinstance(payload, dict):
            return [], None
        markets = [
            Market.from_reward_api(item)
            for item in payload.get("data", [])
            if isinstance(item, dict)
        ]
        return markets[:limit], payload.get("next_cursor")

    async def get_current_rewards(self, limit: int = 500) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            "/rewards/markets/current",
            params={"page_size": min(limit, 500)},
        )
        payload = response.json()
        if isinstance(payload, dict):
            return [item for item in payload.get("data", []) if isinstance(item, dict)]
        return []

    async def get_simplified_markets(self, limit: int = 100) -> list[Market]:
        response = await self._request("GET", "/simplified-markets", params={"limit": limit})
        payload = response.json()
        if isinstance(payload, dict):
            data = payload.get("data", [])
        else:
            data = payload
        return [Market.from_gamma(item) for item in data if isinstance(item, dict)]

    async def get_sampling_markets(self, limit: int = 100) -> list[Market]:
        response = await self._request("GET", "/sampling-markets", params={"limit": limit})
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else payload
        return [Market.from_gamma(item) for item in data if isinstance(item, dict)]

    async def get_spreads(self, token_ids: list[str]) -> dict[str, float]:
        body = [{"token_id": token_id} for token_id in token_ids]
        response = await self._request("POST", "/spreads", json=body)
        payload = response.json()
        if not isinstance(payload, dict):
            return {}
        return {str(token_id): float(spread) for token_id, spread in payload.items()}

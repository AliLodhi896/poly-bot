from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from polybot.config import Settings
from polybot.models import Market


@dataclass(frozen=True)
class SportMetadata:
    sport: str
    series_id: str | None
    tag_ids: tuple[int, ...]
    ordering: str | None = None


class GammaClient:
    """Read-only Gamma API client for market and event discovery."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=settings.gamma_host,
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
        )

    async def __aenter__(self) -> GammaClient:
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
        response = await self._request("GET", "/markets", params={"limit": 1, "active": "true"})
        return response.status_code == 200

    async def list_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        order: str = "volume_24hr",
        ascending: bool = False,
        include_tag: bool = True,
    ) -> list[Market]:
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
            "include_tag": str(include_tag).lower(),
        }
        if active:
            params["active"] = "true"
        response = await self._request("GET", "/markets", params=params)
        payload = response.json()
        if not isinstance(payload, list):
            return []
        markets = [Market.from_gamma(item) for item in payload if isinstance(item, dict)]
        return [market for market in markets if market.condition_id and market.tokens]

    async def iter_markets(self, limit: int = 500, page_size: int = 100) -> AsyncIterator[Market]:
        fetched = 0
        offset = 0
        while fetched < limit:
            batch_size = min(page_size, limit - fetched)
            markets = await self.list_markets(limit=batch_size, offset=offset)
            if not markets:
                break
            for market in markets:
                yield market
            fetched += len(markets)
            offset += batch_size
            if len(markets) < batch_size:
                break

    async def list_events(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        order: str = "volume_24hr",
        ascending: bool = False,
        tag_id: int | None = None,
    ) -> list[dict[str, Any]]:
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if tag_id is not None:
            params["tag_id"] = tag_id
        response = await self._request("GET", "/events", params=params)
        payload = response.json()
        return payload if isinstance(payload, list) else []

    async def list_sports_metadata(self) -> list[SportMetadata]:
        response = await self._request("GET", "/sports")
        payload = response.json()
        result: list[SportMetadata] = []
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict) or not item.get("sport"):
                continue
            tags = tuple(
                int(value.strip())
                for value in str(item.get("tags") or "").split(",")
                if value.strip().isdigit()
            )
            series = item.get("series")
            result.append(
                SportMetadata(
                    sport=str(item["sport"]).lower(),
                    series_id=str(series) if series not in (None, "") else None,
                    tag_ids=tags,
                    ordering=item.get("ordering"),
                )
            )
        return result

    async def get_series_events(self, series_id: str) -> list[dict[str, Any]]:
        response = await self._request("GET", f"/series/{series_id}")
        payload = response.json()
        events = payload.get("events", []) if isinstance(payload, dict) else []
        return [event for event in events if isinstance(event, dict)]

    async def get_event_by_slug(self, slug: str) -> dict[str, Any] | None:
        response = await self._request("GET", f"/events/slug/{slug}")
        payload = response.json()
        return payload if isinstance(payload, dict) else None

    async def discover_markets_via_events(self, limit: int = 100) -> list[Market]:
        events = await self.list_events(limit=limit)
        markets: list[Market] = []
        for event in events:
            event_slug = event.get("slug")
            for raw_market in event.get("markets", []) or []:
                if isinstance(raw_market, dict):
                    raw_market.setdefault("events", [{"slug": event_slug}])
                    markets.append(Market.from_gamma(raw_market))
        return [market for market in markets if market.condition_id and market.tokens]

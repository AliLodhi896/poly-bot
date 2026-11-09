from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel

from polybot.config import Settings

logger = logging.getLogger(__name__)

# A plain, honest User-Agent for the official endpoint — NOT a browser/frontend impersonation.
# We do not try to defeat Cloudflare's bot challenge (that would be circumvention); if it blocks
# the automated request, we simply determine the same fact (the IP's country) another way.
_OFFICIAL_HEADERS = {
    "User-Agent": "polybot/1.0 (+geoblock-precheck)",
    "Accept": "application/json",
}

# Blocked sub-regions within otherwise-accessible countries (best-effort match on region code/name).
# Source: docs.polymarket.com/api-reference/geoblock.
_BLOCKED_REGIONS: dict[str, tuple[str, ...]] = {
    "CA": ("on", "ontario"),
    "UA": ("43", "14", "09", "crimea", "donetsk", "luhansk", "sevastopol"),
}


class GeoblockResult(BaseModel):
    blocked: bool
    ip: str | None = None
    country: str | None = None
    region: str | None = None
    raw: dict[str, object] | None = None


class GeoblockClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
        )

    async def __aenter__(self) -> GeoblockClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._own_client:
            await self.client.aclose()

    async def check(self) -> GeoblockResult:
        """Determine whether this location may place orders on Polymarket.

        Tries Polymarket's authoritative endpoint first (always current). That endpoint sits
        behind Cloudflare and can answer with a bot-challenge (HTML/403) that no header set
        passes; when it does, fall back to an independent IP-geolocation lookup checked against
        Polymarket's published restricted-jurisdiction list, so the compliance gate keeps working
        instead of crashing the trading start. A failed fallback fails closed (caller blocks).
        """
        official = await self._check_official()
        if official is not None:
            return official
        if not self.settings.geoblock_fallback_enabled:
            raise RuntimeError(
                "geoblock endpoint unreachable and POLYBOT_GEOBLOCK_FALLBACK_ENABLED=false; "
                "cannot verify jurisdiction, refusing to trade"
            )
        return await self._check_fallback()

    async def _check_official(self) -> GeoblockResult | None:
        """Authoritative result from Polymarket, or None if the endpoint did not answer cleanly."""
        try:
            response = await self.client.get(self.settings.geoblock_url, headers=_OFFICIAL_HEADERS)
            if response.status_code != 200:
                return None
            payload = response.json()
        except Exception:  # noqa: BLE001 — any non-JSON/challenge/network error -> use fallback
            return None
        if not isinstance(payload, dict) or "blocked" not in payload:
            return None
        return GeoblockResult(
            blocked=bool(payload.get("blocked", True)),
            ip=payload.get("ip"),
            country=payload.get("country"),
            region=payload.get("region"),
            raw=payload,
        )

    async def _check_fallback(self) -> GeoblockResult:
        """Independent IP geolocation vs Polymarket's restricted-jurisdiction list."""
        response = await self.client.get(
            self.settings.geoblock_ip_lookup_url, headers={"User-Agent": "polybot-geoblock"}
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            data = {}
        country = str(
            data.get("countryCode") or data.get("country_code") or data.get("country") or ""
        ).upper()
        region_code = str(data.get("region_code") or data.get("regionCode") or "")
        region_name = str(data.get("region") or data.get("regionName") or "")
        ip = str(data.get("ip") or data.get("query") or "") or None

        if not country:
            # Could not determine the country -> fail closed.
            blocked = True
        else:
            blocked = country in self.settings.geoblock_blocked_country_set
            if not blocked and country in _BLOCKED_REGIONS:
                rc = region_code.strip().lower()
                rn = region_name.strip().lower()
                blocked = any(token == rc or token in rn for token in _BLOCKED_REGIONS[country])

        logger.warning(
            "geoblock: official endpoint unavailable; IP fallback country=%s region=%s blocked=%s",
            country or "?",
            region_name or region_code or "?",
            blocked,
        )
        return GeoblockResult(
            blocked=blocked,
            ip=ip,
            country=country or None,
            region=region_name or region_code or None,
            raw={"source": "ip-fallback", "country": country, "region": region_name},
        )

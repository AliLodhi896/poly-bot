from __future__ import annotations

import json

import httpx
import pytest

from polybot.api.geoblock import GeoblockClient
from polybot.config import Settings


def _client(handler) -> tuple[GeoblockClient, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    settings = Settings(_env_file=None)
    return GeoblockClient(settings, client=http), http


def _cloudflare_challenge() -> httpx.Response:
    return httpx.Response(403, text="<!DOCTYPE html><title>Just a moment...</title>")


def _ipgeo(country: str, region: str = "", region_code: str = "") -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(
            {"country_code": country, "region": region, "region_code": region_code, "ip": "1.2.3.4"}
        ),
        headers={"content-type": "application/json"},
    )


def _run(handler):
    import asyncio

    gc, http = _client(handler)

    async def go():
        try:
            return await gc.check()
        finally:
            await http.aclose()

    return asyncio.run(go())


def test_fallback_allows_unblocked_country_when_endpoint_is_cloudflared() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geoblock" in str(request.url):
            return _cloudflare_challenge()
        return _ipgeo("GE", region="Tbilisi")  # Georgia is not restricted

    result = _run(handler)
    assert result.blocked is False
    assert result.country == "GE"
    assert result.raw and result.raw.get("source") == "ip-fallback"


def test_fallback_blocks_restricted_country() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geoblock" in str(request.url):
            return _cloudflare_challenge()
        return _ipgeo("US", region="New York")

    assert _run(handler).blocked is True


def test_fallback_blocks_close_only_country() -> None:
    # The reward bot opens maker orders, which close-only jurisdictions (e.g. Singapore) forbid.
    def handler(request: httpx.Request) -> httpx.Response:
        if "geoblock" in str(request.url):
            return _cloudflare_challenge()
        return _ipgeo("SG")

    assert _run(handler).blocked is True


def test_fallback_blocks_ontario_region() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geoblock" in str(request.url):
            return _cloudflare_challenge()
        return _ipgeo("CA", region="Ontario", region_code="ON")

    result = _run(handler)
    assert result.blocked is True and result.country == "CA"


def test_official_endpoint_is_authoritative_when_reachable() -> None:
    # When Polymarket answers cleanly, the IP fallback is not consulted (even when the
    # verdict is "blocked").
    def handler(request: httpx.Request) -> httpx.Response:
        if "geoblock" in str(request.url):
            return httpx.Response(200, json={"blocked": False, "country": "PT", "ip": "9.9.9.9"})
        raise AssertionError("fallback must not be called when the official endpoint answers")

    result = _run(handler)
    assert result.blocked is False and result.country == "PT"


def test_empty_country_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geoblock" in str(request.url):
            return _cloudflare_challenge()
        return _ipgeo("")  # geolocation could not resolve a country

    assert _run(handler).blocked is True


def test_fallback_disabled_raises_instead_of_silently_allowing() -> None:
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return _cloudflare_challenge()

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    settings = Settings(_env_file=None, geoblock_fallback_enabled=False)
    gc = GeoblockClient(settings, client=http)

    async def go():
        try:
            with pytest.raises(RuntimeError):
                await gc.check()
        finally:
            await http.aclose()

    asyncio.run(go())

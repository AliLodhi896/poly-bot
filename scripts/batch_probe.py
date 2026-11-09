#!/usr/bin/env python3
"""One-shot probe to capture the RAW response shape of the CLOB batch ``POST /orders``.

We need this because the batch-post response format is undocumented, and parsing order ids
wrong on the live path risks double-placement. The probe is deliberately harmless: it places
two post-only BUY orders at price 0.01 (one tick — deep below mid, cannot fill), prints the raw
response so we can see its structure, then cancels everything.

Run (live gates must be enabled, like the bot; remove .polybot-kill first):

    uv run python scripts/batch_probe.py

Paste the printed "RAW post_orders response" block back so batch placement can be wired up safely.
"""

from __future__ import annotations

import asyncio
import contextlib

from polybot.api.clob import ClobClient
from polybot.config import get_settings
from polybot.execution.gateway import LiveOrderGateway


async def _find_market(settings):
    """Pick any reward market with a live book; return (tokens, tick, min_order_size, neg_risk)."""
    async with ClobClient(settings) as clob:
        raw, _ = await clob.get_rewards_markets(
            limit=40, order_by="rate_per_day", position="DESC"
        )
        for market in raw:
            cid = getattr(market, "condition_id", "")
            tokens = getattr(market, "tokens", [])
            if not cid or len(tokens) < 2:
                continue
            info = await clob.get_clob_market_info(cid)
            book = await clob.get_order_book(tokens[0].token_id)
            if book.midpoint is None:
                continue
            return tokens, float(info.tick_size), float(info.min_order_size), info.neg_risk
    return None


async def main() -> None:
    settings = get_settings()
    found = await _find_market(settings)
    if found is None:
        print("No reward market with a book found — try again later.")
        return
    tokens, tick, min_order_size, neg_risk = found
    price = tick  # 0.01 — the deepest valid price; a post-only BUY here will rest, never fill
    size = max(min_order_size, 5.0)
    specs = [
        (tokens[0].token_id, "BUY", price, size, tick, neg_risk),
        (tokens[1].token_id, "BUY", price, size, tick, neg_risk),
    ]

    gateway = LiveOrderGateway(settings)
    await gateway.start()  # enforces LIVE_ENABLED, kill switch, geoblock, credentials
    print(f"Placing 2 post-only BUY @ {price:g} x{size:g} (deep below mid — will NOT fill)…")
    try:
        response = gateway.post_orders_raw(specs)
        print("\n=== RAW post_orders response ===")
        print("type:", type(response).__name__)
        print("repr:", repr(response)[:2000])
        order_ids = []
        if isinstance(response, list):
            for i, el in enumerate(response):
                shape = list(el.keys()) if isinstance(el, dict) else repr(el)[:120]
                print(f"  [{i}] type={type(el).__name__} -> {shape}")
                if isinstance(el, dict) and el.get("orderID"):
                    order_ids.append(str(el["orderID"]))
        print("=== end ===\n")

        # --- CANCEL probe: cancel the just-placed orders and see if they actually disappear ---
        if order_ids:
            print(f"Cancelling {len(order_ids)} order(s) via cancel_orders…")
            cancel_resp = gateway.cancel_orders_raw(order_ids)
            print("=== RAW cancel_orders response ===")
            print("type:", type(cancel_resp).__name__)
            print("repr:", repr(cancel_resp)[:2000])
            if isinstance(cancel_resp, dict):
                print("dict keys:", list(cancel_resp.keys()))
            print("=== end ===\n")
            # Poll open_orders to measure how long they linger (lag) vs truly gone.
            placed = set(order_ids)
            for attempt in range(4):
                await asyncio.sleep(1.0)
                resting = {oid for oid, _tok in gateway.open_orders()}
                still = placed & resting
                print(f"  +{attempt + 1}s: {len(still)}/{len(placed)} of our orders still resting")
                if not still:
                    print("  → all gone (cancel confirmed; any earlier 'orphan' is read lag).")
                    break
    except Exception as exc:  # noqa: BLE001 — surface whatever the CLOB returned
        print(f"probe raised: {type(exc).__name__}: {exc}")
    finally:
        print("Final cleanup: cancel_all…")
        with contextlib.suppress(Exception):
            gateway.cancel_all()
        print("Done. (Verify on Polymarket that no orders remain.)")


if __name__ == "__main__":
    asyncio.run(main())

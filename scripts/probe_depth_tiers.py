"""Read-only probe: book depth inside the reward zone + volatility, grouped by min_shares.

Pulls live reward markets, samples a handful per min_shares tier, and for each measures how
many shares rest inside the reward zone (mid +/- max_spread) on the BUY side of BOTH legs (we
quote BUY YES and BUY NO), how many ticks the zone spans, and the recent price range. No orders
are ever placed — only public GET endpoints are used.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections import defaultdict
from decimal import Decimal

from polybot.api.clob import ClobClient
from polybot.config import Settings
from polybot.feed.books import FastBook
from polybot.rewards.models import normalize_reward_spread

TARGET_TIERS = [20, 50, 100, 200, 250, 500, 1000]
SAMPLE_PER_TIER = 18


def _zone_buy_depth(book: FastBook, mid: float, max_spread: float) -> float:
    """Cumulative bid shares inside the reward zone — the max protective buffer while scoring."""
    tick = float(book.tick_size or Decimal("0.01"))
    floor_price = round((mid - max_spread) / tick) * tick
    return float(book.cumulative_depth_ahead("BUY", floor_price))


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


async def main() -> None:
    settings = Settings()
    async with ClobClient(settings) as clob:
        markets, _ = await clob.get_rewards_markets(limit=500, order_by="rate_per_day")
        rows = []
        for m in markets:
            spread = normalize_reward_spread(m.rewards.max_spread)
            if spread is None or spread <= 0 or len(m.tokens) < 2:
                continue
            rows.append(
                {
                    "cond": m.condition_id,
                    "min": float(m.rewards.min_size),
                    "spread": float(spread),
                    "change": abs(float(getattr(m, "one_day_price_change", 0.0) or 0.0)),
                    "ta": m.tokens[0].token_id,
                    "tb": m.tokens[1].token_id,
                }
            )

        by_tier: dict[float, list[dict]] = defaultdict(list)
        for r in rows:
            by_tier[r["min"]].append(r)

        print(f"fetched {len(rows)} reward markets; min_size tiers present:")
        for tier in sorted(by_tier):
            print(f"  min={tier:>6g}: {len(by_tier[tier])} markets")
        print()

        # Sample per requested tier (plus any present tier we listed), then fetch books once.
        sampled: list[dict] = []
        for tier in sorted(by_tier):
            if tier in TARGET_TIERS:
                sampled.extend(by_tier[tier][:SAMPLE_PER_TIER])
        tokens = [r["ta"] for r in sampled] + [r["tb"] for r in sampled]
        books = await clob.get_order_books(tokens)

        async def vol(token: str) -> float:
            hist = await clob.price_history(token, interval="6h", fidelity=1)
            prices = [p for _, p in hist]
            return (max(prices) - min(prices)) if len(prices) >= 2 else float("nan")

        vols = await asyncio.gather(*(vol(r["ta"]) for r in sampled))

        CALM_MAX_ZONES = 2.0  # 6h move <= 2 reward-zone widths => farmable, not blown out
        agg: dict[float, dict[str, list[float]]] = defaultdict(
            lambda: {"mult": [], "spread_c": [], "range6h": [], "seen": [], "volatile": []}
        )
        skipped = 0
        for r, v6h in zip(sampled, vols, strict=True):
            ba, bb = books.get(r["ta"]), books.get(r["tb"])
            if ba is None or bb is None:
                skipped += 1
                continue
            fa, fb = FastBook.from_book(ba), FastBook.from_book(bb)
            ma, mb = fa.midpoint(), fb.midpoint()
            if ma is None or mb is None:
                skipped += 1
                continue
            mid_a, mid_b = float(ma), float(mb)
            tick = float(fa.tick_size or Decimal("0.01"))
            zone_ticks = r["spread"] / tick
            if tick > 0.0101 or zone_ticks > 12 or not (0.05 <= mid_a <= 0.95):
                skipped += 1
                continue
            agg[r["min"]]["seen"].append(1.0)
            range_zones = (v6h / r["spread"]) if (v6h == v6h and r["spread"]) else 0.0
            if range_zones > CALM_MAX_ZONES:
                agg[r["min"]]["volatile"].append(1.0)
                continue  # too volatile to farm — exclude from the depth/mult stats
            both_leg = min(
                _zone_buy_depth(fa, mid_a, r["spread"]),
                _zone_buy_depth(fb, mid_b, r["spread"]),
            )
            agg[r["min"]]["mult"].append(both_leg / r["min"] if r["min"] > 0 else 0.0)
            agg[r["min"]]["spread_c"].append(r["spread"] * 100)
            if v6h == v6h:
                agg[r["min"]]["range6h"].append(v6h * 100)

        print(f"standard 1c-tick regime (skipped {skipped} exotic/extreme of {len(sampled)});")
        print(f"depth stats are CALM markets only (6h move <= {CALM_MAX_ZONES:g} zone-widths)")
        print()
        print(
            f"{'min':>6} {'calm':>5} {'volatile':>8}  achievable mult (calm)      "
            f"{'spread¢':>7}  {'6h_rng¢':>7}"
        )
        print(f"{'':>21}  {'p25':>6} {'p50':>6} {'p75':>6}")
        print("-" * 78)
        for tier in sorted(agg):
            a = agg[tier]
            seen = len(a["seen"])
            if not seen:
                continue
            nvol = len(a["volatile"])
            m = a["mult"]
            mp = (
                f"{_pct(m,.25):>6.0f} {_pct(m,.5):>6.0f} {_pct(m,.75):>6.0f}"
                if m
                else f"{'—':>6} {'—':>6} {'—':>6}"
            )
            sp = f"{statistics.median(a['spread_c']):>7.1f}" if a["spread_c"] else f"{'—':>7}"
            rg = f"{_pct(a['range6h'],.5):>7.1f}" if a["range6h"] else f"{'—':>7}"
            print(f"{tier:>6g} {len(m):>5} {nvol:>8}  {mp}   {sp}  {rg}")
        print(
            f"\n(probed {time.strftime('%Y-%m-%d %H:%M:%S')}; mult = both-leg in-zone bid depth "
            f"/ min_shares; 'volatile' = excluded markets moving >{CALM_MAX_ZONES:g} zones/6h)"
        )


if __name__ == "__main__":
    asyncio.run(main())

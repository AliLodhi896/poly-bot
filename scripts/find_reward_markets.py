#!/usr/bin/env python3
"""Scan the live Polymarket reward pool and print the best markets to farm.

    Thin CLI wrapper around :func:`polybot.rewards.scanner.scan_reward_markets`.
The scoring lives in the reusable module so the live bot can later import it for
a continuous market-rotation watcher.

Usage:
    uv run python scripts/find_reward_markets.py
    uv run python scripts/find_reward_markets.py --top 5 --json
    uv run python scripts/find_reward_markets.py --no-prefer-weather --min-hours 24
"""

from __future__ import annotations

import argparse
import asyncio
import json

from polybot.api.clob import ClobClient
from polybot.config import get_settings
from polybot.rewards.scanner import ScanConfig, ScoredMarket, scan_reward_markets


def _build_config(args: argparse.Namespace, settings) -> ScanConfig:
    overrides: dict[str, object] = {
        "top": args.top,
        "max_min_size": args.max_min_size,
        "min_hours": args.min_hours,
        "size_shares": args.size_shares,
    }
    if args.min_midpoint is not None:
        overrides["min_midpoint"] = args.min_midpoint
    if args.max_1d_change is not None:
        overrides["max_one_day_change"] = args.max_1d_change
    if args.min_depth is not None:
        overrides["min_top_depth"] = args.min_depth
    if args.pool_limit is not None:
        overrides["pool_limit"] = args.pool_limit
    if args.max_step is not None:
        overrides["max_recent_step"] = args.max_step
    if args.max_range is not None:
        overrides["max_recent_range"] = args.max_range
    return ScanConfig.from_settings(settings, **overrides)


def _fmt_hours(h: float | None) -> str:
    if h is None:
        return "  n/a"
    if h >= 48:
        return f"{h / 24:4.0f}d"
    return f"{h:4.0f}h"


def _print_table(rows: list[ScoredMarket]) -> None:
    if not rows:
        print("No markets passed the filters. Loosen --min-depth / --min-hours / --max-1d-change.")
        return
    header = (
        f"{'#':>2}  {'score':>6} {'proj$/d':>7} {'risk':>5} {'rate':>5} {'share':>6} "
        f"{'comp':>6} {'mid':>5} {'left':>5} {'1dΔ':>5}  {'tier':<8} question"
    )
    print(header)
    print("-" * len(header))
    for i, m in enumerate(rows, 1):
        print(
            f"{i:>2}  {m.score:6.2f} {m.projected_reward:7.2f} {m.risk_factor:5.2f} "
            f"{m.rate_per_day:5.0f} {m.share * 100:5.2f}% {m.competition:6.0f} {m.mid:5.2f} "
            f"{_fmt_hours(m.hours_left):>5} {m.one_day_change:+5.2f}  {m.risk_tier:<8} "
            f"{m.question[:42]}"
        )
        print(f"      {m.condition_id}")


def _to_dict(m: ScoredMarket) -> dict[str, object]:
    return {
        "condition_id": m.condition_id,
        "question": m.question,
        "category": m.category,
        "mid": round(m.mid, 4),
        "rate_per_day": m.rate_per_day,
        "min_size": m.min_size,
        "max_spread": m.max_spread,
        "competition": round(m.competition, 2),
        "top_depth": round(m.top_depth, 2),
        "depth_ratio": round(m.depth_ratio, 3),
        "required_size": round(m.required_size, 2),
        "quote_size": round(m.quote_size, 2),
        "size_eligible": m.size_eligible,
        "hours_left": None if m.hours_left is None else round(m.hours_left, 2),
        "one_day_change": m.one_day_change,
        "volume_24hr": m.volume_24hr,
        "projected_reward": round(m.projected_reward, 4),
        "share": round(m.share, 5),
        "risk_factor": round(m.risk_factor, 4),
        "risk_tier": m.risk_tier,
        "score": round(m.score, 4),
        "volatility": round(m.volatility, 4),
        "max_step": round(m.max_step, 4),
        "volatility_10m": round(m.volatility_10m, 4),
        "volatility_2m": round(m.volatility_2m, 4),
        "max_step_2m": round(m.max_step_2m, 4),
        "flags": m.flags,
        "token_a": m.token_a,
        "token_b": m.token_b,
    }


async def _run(args: argparse.Namespace) -> list[ScoredMarket]:
    settings = get_settings()
    cfg = _build_config(args, settings)
    async with ClobClient(settings) as clob:
        return await scan_reward_markets(clob, cfg)


def main() -> None:
    p = argparse.ArgumentParser(description="Scan Polymarket reward markets and rank the best.")
    p.add_argument("--top", type=int, default=5, help="How many markets to return (default 5).")
    p.add_argument(
        "--max-min-size",
        type=float,
        default=0.0,
        help="Legacy max reward min_size gate; 0 shows all min sizes.",
    )
    p.add_argument("--min-midpoint", type=float, default=None, help="Reject extreme mids.")
    p.add_argument("--min-hours", type=float, default=12.0, help="Min hours to resolution.")
    p.add_argument("--max-1d-change", type=float, default=None, help="Max |1-day price change|.")
    p.add_argument("--min-depth", type=float, default=None, help="Min top-of-book depth (shares).")
    p.add_argument("--size-shares", type=float, default=20.0, help="Per-quote size estimate.")
    p.add_argument("--pool-limit", type=int, default=None, help="How many reward markets to pull.")
    p.add_argument("--max-step", type=float, default=None, help="Reject fast jumps over this.")
    p.add_argument("--max-range", type=float, default=None, help="Reject mid swing over this.")
    p.add_argument(
        "--no-prefer-weather",
        action="store_true",
        help="Deprecated no-op: the scanner no longer applies a category bias (ranks on merit).",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p.add_argument("--env", action="store_true", help="Print a paste-ready whitelist env line.")
    args = p.parse_args()

    rows = asyncio.run(_run(args))

    if args.json:
        print(json.dumps([_to_dict(m) for m in rows], indent=2, ensure_ascii=False))
    else:
        _print_table(rows)

    if args.env and rows:
        ids = ",".join(m.condition_id for m in rows)
        print("\n# paste into your launch command:")
        print(f"POLYBOT_REWARD_MM_MARKET_WHITELIST={ids}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal

import pytest

from polybot.config import Settings
from polybot.models import Book, Level
from polybot.rewards.models import RewardPair
from polybot.rewards.scanner import ScanConfig
from polybot.rewards.selector import (
    SelectedMarket,
    SelectorConfig,
    _group_unique_by_category,
    evaluate,
)

NOW = time.time()
SETTINGS = Settings(_env_file=None)


def _cfg(size_shares: float = 50.0) -> SelectorConfig:
    return SelectorConfig.from_settings(SETTINGS, size_shares=size_shares)


def _scan_cfg(size_shares: float = 50.0) -> ScanConfig:
    return ScanConfig.from_settings(SETTINGS, size_shares=size_shares, depth_based=True)


def _pair(
    *,
    question: str = "Will the high temp in Denver be 80-82F?",
    min_size: float = 50.0,
    max_spread: float = 0.045,
    rate: float = 150.0,
    one_day_change: float = 0.0,
    hours_to_end: float | None = 48.0,
    game_start_in_hours: float | None = None,
) -> RewardPair:
    end = datetime.fromtimestamp(NOW + hours_to_end * 3600) if hours_to_end is not None else None
    gs = (
        datetime.fromtimestamp(NOW + game_start_in_hours * 3600)
        if game_start_in_hours is not None
        else None
    )
    return RewardPair(
        condition_id="0xabc",
        question=question,
        event_id="e1",
        event_slug="evt",
        outcome_a="Yes",
        outcome_b="No",
        token_a="yes-token",
        token_b="no-token",
        volume_24hr=4000.0,
        market_spread=0.01,
        rewards_min_size=min_size,
        rewards_max_spread=max_spread,
        rewards_rate_per_day=rate,
        tick_size=Decimal("0.01"),
        min_order_size=5.0,
        one_day_price_change=one_day_change,
        end_date=end,
        game_start_time=gs,
    )


def _book(asset_id: str, bids: dict[str, str], asks: dict[str, str]) -> Book:
    return Book(
        asset_id=asset_id,
        bids=[Level(price=Decimal(p), size=Decimal(s)) for p, s in bids.items()],
        asks=[Level(price=Decimal(p), size=Decimal(s)) for p, s in asks.items()],
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
    ).sorted()


# Calm, deep, mid 0.50 book on both legs: zone [0.455, 0.50], 800/level clears the 750 floor and
# stacks a big in-zone buffer.
def _deep(asset_id: str) -> Book:
    return _book(
        asset_id,
        bids={"0.49": "800", "0.48": "800", "0.47": "800", "0.46": "800"},
        asks={"0.51": "800"},
    )


def _thin(asset_id: str) -> Book:
    return _book(
        asset_id,
        bids={"0.49": "50", "0.48": "50", "0.47": "50", "0.46": "50"},
        asks={"0.51": "50"},
    )


def _selected(condition_id: str, category: str, rank: float) -> SelectedMarket:
    return SelectedMarket(
        condition_id=condition_id,
        question=f"{condition_id}?",
        category=category,
        rate_per_day=100.0,
        projected_reward=1.0,
        rank_reward=rank,
        min_size=20.0,
        required_size=20.0,
        size_eligible=True,
        mid=0.5,
        safety=90.0,
        grade="A",
        risk_label="calm",
        token_a=f"{condition_id}-yes",
        token_b=f"{condition_id}-no",
    )


def _eval(pair, ba, bb, *, category="weather", vol=(0.01, 0.005), size=50.0):
    return evaluate(
        pair, ba, bb, category=category, vol=vol, now=NOW, cfg=_cfg(size), scan_cfg=_scan_cfg(size)
    )


def test_group_unique_by_category_dedupes_condition_ids() -> None:
    grouped = _group_unique_by_category(
        [
            _selected("0xaaa", "weather", 3.0),
            _selected("0xbbb", "weather", 2.0),
            _selected("0xaaa", "weather", 1.0),
            _selected("0xbbb", "finance", 5.0),
            _selected("0xccc", "finance", 4.0),
        ],
        per_category=10,
    )

    assert [m.condition_id for m in grouped["weather"]] == ["0xaaa", "0xbbb"]
    assert [m.condition_id for m in grouped["finance"]] == ["0xccc"]


def test_calm_deep_market_passes_with_high_safety() -> None:
    pair = _pair()
    market, reason = _eval(pair, _deep(pair.token_a), _deep(pair.token_b))
    assert reason == ""
    assert market is not None
    assert market.grade == "A"
    assert market.safety >= 75.0
    assert market.vol_known
    assert market.risk_label == "calm · deep book"
    assert market.sweep_lots > 20.0  # in-zone depth well above the target


def test_gap_risk_market_excluded() -> None:
    pair = _pair(question="Will Caleb Wilson be the 4th overall pick in the 2026 NBA Draft?")
    market, reason = _eval(pair, _deep(pair.token_a), _deep(pair.token_b), category="sports")
    assert market is None
    assert reason == "gap_risk"


def test_politics_excluded_wholesale() -> None:
    pair = _pair(question="Who will be the next Prime Minister of Romania?")
    market, reason = _eval(pair, _deep(pair.token_a), _deep(pair.token_b), category="politics")
    assert market is None
    assert reason == "gap_risk"


def test_live_or_near_start_sports_excluded_but_pregame_prop_allowed() -> None:
    live = _pair(question="Will Team A beat Team B?", game_start_in_hours=0.1)
    m1, r1 = _eval(live, _deep(live.token_a), _deep(live.token_b), category="sports")
    assert m1 is None and r1 == "live_sports"
    # Clockless pre-game prop (no game_start_time) is allowed.
    prop = _pair(question="Will the announcers say 'Champion' during the broadcast?")
    m2, r2 = _eval(prop, _deep(prop.token_a), _deep(prop.token_b), category="sports")
    assert m2 is not None and r2 == ""


def test_near_resolution_excluded() -> None:
    pair = _pair(hours_to_end=6.0)
    _m, reason = _eval(pair, _deep(pair.token_a), _deep(pair.token_b))
    assert reason == "near_resolution"


def test_weather_uses_shorter_near_resolution_gate() -> None:
    # A market 14h from resolution: weather passes (12h gate), every other category fails
    # (18h gate).
    pair = _pair(hours_to_end=14.0)
    mw, rw = _eval(pair, _deep(pair.token_a), _deep(pair.token_b), category="weather")
    assert mw is not None and rw == ""
    mo, ro = _eval(pair, _deep(pair.token_a), _deep(pair.token_b), category="other")
    assert mo is None and ro == "near_resolution"
    # Weather still excluded below its own 12h gate.
    near = _pair(hours_to_end=10.0)
    _m, rn = _eval(near, _deep(near.token_a), _deep(near.token_b), category="weather")
    assert rn == "near_resolution"


def test_one_day_move_excluded() -> None:
    pair = _pair(one_day_change=0.22)
    _m, reason = _eval(pair, _deep(pair.token_a), _deep(pair.token_b))
    assert reason == "volatile_1d"


def test_too_small_for_min_size_excluded() -> None:
    pair = _pair(min_size=100.0)
    _m, reason = _eval(pair, _deep(pair.token_a), _deep(pair.token_b), size=50.0)
    assert reason == "too_small"


def test_extreme_price_excluded() -> None:
    # mid 0.05 -> outside [0.10, 0.90]
    pair = _pair(max_spread=0.03)
    ba = _book(pair.token_a, bids={"0.04": "800", "0.03": "800"}, asks={"0.06": "800"})
    bb = _book(pair.token_b, bids={"0.94": "800", "0.93": "800"}, asks={"0.96": "800"})
    _m, reason = _eval(pair, ba, bb)
    assert reason == "extreme_price"


def test_thin_book_excluded_by_depth_feasibility() -> None:
    pair = _pair()
    _m, reason = _eval(pair, _thin(pair.token_a), _thin(pair.token_b))
    assert reason == "thin_for_size"


def test_high_realized_volatility_excluded() -> None:
    pair = _pair()
    _m, reason = _eval(pair, _deep(pair.token_a), _deep(pair.token_b), vol=(0.20, 0.01))
    assert reason == "volatile"


def test_missing_volatility_is_not_treated_as_calm() -> None:
    pair = _pair()
    market, reason = _eval(pair, _deep(pair.token_a), _deep(pair.token_b), vol=None)
    assert reason == ""
    assert market is not None
    assert not market.vol_known
    assert market.risk_label == "⚠ vol unknown"
    assert market.safety < 90.0  # the unknown-vol penalty pulls the calm sub-score to 0.5


def test_gap_risk_token_launch_excluded() -> None:
    pair = _pair(question="Will Saturn launch a token by December 2026?")
    _m, reason = _eval(pair, _deep(pair.token_a), _deep(pair.token_b), category="finance")
    assert reason == "gap_risk"


def test_gap_risk_geopolitics_macro_and_event_patterns_excluded() -> None:
    # Previously-leaking high-min event markets must now be caught by the gap-risk gate.
    for q in (
        "Will the Strait of Hormuz traffic return to normal by July?",
        "Fed Rate Hike by July 2026 Meeting?",
        "Will Starmer officially leave office by 2026?",
        "US strike on Cuba by December 31?",
        "Will GPT-5.6 be released by July 31, 2026?",
        "Will M80 win the CSGO ACE Stage 3?",
    ):
        pair = _pair(question=q)
        _m, reason = _eval(pair, _deep(pair.token_a), _deep(pair.token_b), category="other")
        assert reason == "gap_risk", q


def test_gap_risk_political_appointment_excluded_even_in_finance_bucket() -> None:
    # A finance/economy tag can mislabel a political appointment as 'finance'; the gap-risk gate
    # must still exclude it (appointments reprice on a single announcement).
    pair = _pair(question="Will Yvette Cooper be the next Chancellor of the Exchequer?")
    _m, reason = _eval(pair, _deep(pair.token_a), _deep(pair.token_b), category="finance")
    assert reason == "gap_risk"


def test_size_fit_bonus_is_exact_match_only() -> None:
    # At size 50, ONLY a min-50 market gets the size-fit bonus (x1.25 with default k=0.25); a
    # min-20 market keeps its raw reward (no partial boost). projected itself is unchanged.
    big = _pair(min_size=50.0)
    small = _pair(min_size=20.0)
    mb, _b = _eval(big, _deep(big.token_a), _deep(big.token_b), size=50.0)
    ms, _s = _eval(small, _deep(small.token_a), _deep(small.token_b), size=50.0)
    assert mb is not None and ms is not None
    assert mb.rank_reward == pytest.approx(mb.projected_reward * 1.25)
    assert ms.rank_reward == pytest.approx(ms.projected_reward)  # no bonus for the off-tier market


def test_cheap_gates_run_before_book_fetch() -> None:
    # With books absent, a market that passes the cheap gates reports 'no_book' (it is a hydrate
    # candidate); a market failing a cheap gate reports that gate first.
    ok = _pair()
    _m, reason = evaluate(
        ok, None, None, category="weather", vol=None, now=NOW, cfg=_cfg(), scan_cfg=_scan_cfg()
    )
    assert reason == "no_book"
    bad = _pair(question="NBA Draft: 1st pick?")
    _m2, reason2 = evaluate(
        bad, None, None, category="sports", vol=None, now=NOW, cfg=_cfg(), scan_cfg=_scan_cfg()
    )
    assert reason2 == "gap_risk"

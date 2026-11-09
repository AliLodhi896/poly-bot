from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from polybot.config import Settings
from polybot.feed.books import FastBook
from polybot.models import Book, Level
from polybot.rewards.engine import RewardMMEngine
from polybot.rewards.models import RewardPair
from polybot.rewards.scanner import (
    ScanConfig,
    _depth_farm_assessment,
    _risk_tier,
    _score_pair,
    classify_category,
)
from polybot.storage import Storage


def _pair() -> RewardPair:
    # min_shares 20 -> depth tier (25/35/45): floor buffer 500, target 700.
    return RewardPair(
        condition_id="0xabc",
        question="Will the scanner farmability match live?",
        event_id="event-1",
        event_slug="reward-event",
        outcome_a="Yes",
        outcome_b="No",
        token_a="yes-token",
        token_b="no-token",
        volume_24hr=5000.0,
        market_spread=0.01,
        rewards_min_size=20.0,
        rewards_max_spread=0.045,
        rewards_rate_per_day=100.0,
        tick_size=Decimal("0.01"),
        min_order_size=5.0,
    )


def _book(asset_id: str, bids: dict[str, str], asks: dict[str, str]) -> Book:
    return Book(
        asset_id=asset_id,
        bids=[Level(price=Decimal(p), size=Decimal(s)) for p, s in bids.items()],
        asks=[Level(price=Decimal(p), size=Decimal(s)) for p, s in asks.items()],
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
    ).sorted()


# mid 0.385, zone floor 0.34, post-only ceiling 0.38. 600 ahead at 0.38 clears the 500 floor.
def _thick(asset_id: str) -> Book:
    return _book(
        asset_id,
        bids={"0.38": "600", "0.37": "600", "0.36": "600", "0.35": "600", "0.34": "600"},
        asks={"0.39": "600"},
    )


# 50 per level: the whole in-zone ladder (~250) never clears the 500 floor -> unfarmable.
def _thin(asset_id: str) -> Book:
    return _book(
        asset_id,
        bids={"0.38": "50", "0.37": "50", "0.36": "50", "0.35": "50", "0.34": "50"},
        asks={"0.39": "50"},
    )


def _depth_cfg(**overrides: object) -> ScanConfig:
    return replace(ScanConfig(top=5, depth_based=True, size_shares=20.0), **overrides)


def test_depth_mode_scores_farmable_market_with_real_buffer() -> None:
    pair = _pair()
    books = {pair.token_a: _thick(pair.token_a), pair.token_b: _thick(pair.token_b)}
    scored = _score_pair(pair, books, _depth_cfg())
    assert scored is not None
    # 0.38 sits at the back of the best bid: 600 shares ahead, clearing the 500 floor.
    assert scored.depth_buffer_shares == 600.0
    assert scored.sweep_shares == 600.0
    assert "depth-farmable" in scored.flags
    # The top-of-book "thin" badge is a non-depth proxy and must not fire in depth mode.
    assert "thin" not in scored.flags


def test_depth_mode_drops_market_too_thin_for_size() -> None:
    pair = _pair()
    # One thin leg is enough: the live planner would not quote it, so the scanner drops it.
    books = {pair.token_a: _thick(pair.token_a), pair.token_b: _thin(pair.token_b)}
    assert _score_pair(pair, books, _depth_cfg()) is None


def test_legacy_mode_unchanged_when_depth_flag_off() -> None:
    pair = _pair()
    books = {pair.token_a: _thin(pair.token_a), pair.token_b: _thin(pair.token_b)}
    # Disable the top-of-book gate so the depth feasibility is the only thing that could drop it;
    # with depth OFF the thin book still scores via the legacy proxy and reports no depth buffer.
    cfg = replace(ScanConfig(top=5), depth_based=False, min_top_depth=0.0, size_shares=20.0)
    scored = _score_pair(pair, books, cfg)
    assert scored is not None
    assert scored.depth_buffer_shares == 0.0
    assert "depth-farmable" not in scored.flags


def test_draft_and_pick_markets_classified_gap_risk() -> None:
    cfg = ScanConfig()
    # The exact run48 culprit and the user's pinned "drafted in the top N" props.
    assert _risk_tier(
        "Will Caleb Wilson be the 4th overall pick in the 2026 NBA Draft?",
        False,
        cfg,
        category="sports",
    ) == "gap-risk"
    assert _risk_tier(
        "Will Bennett Stirtz be drafted in the top 10?", False, cfg, category="sports"
    ) == "gap-risk"
    # A plain head-to-head game market is not a single-headline gap.
    assert _risk_tier("Will the Lakers beat the Celtics?", False, cfg, category="sports") == "event"


def test_assessment_matches_live_engine_leg_verdict(tmp_path) -> None:
    pair = _pair()
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "scan.sqlite3",
        reward_mm_depth_based_placement=True,
    )
    storage = Storage(settings.database_path)
    storage.init()
    engine = RewardMMEngine(settings, storage)

    for label, book_b in (("thick", _thick(pair.token_b)), ("thin", _thin(pair.token_b))):
        book_a = _thick(pair.token_a)
        assessment = _depth_farm_assessment(pair, book_a, book_b, _depth_cfg(), 20.0)
        engine_farmable = engine._leg_farmable_by_depth(
            pair, FastBook.from_book(book_a), "a", pair.outcome_a, 20.0
        ) and engine._leg_farmable_by_depth(
            pair, FastBook.from_book(book_b), "b", pair.outcome_b, 20.0
        )
        assert assessment.farmable is engine_farmable, label


def test_classify_category_tags_beat_keywords_and_cover_geopolitics() -> None:
    # Metadata-first: the event's Polymarket tags decide the bucket. Geopolitics tags -> politics;
    # a finance tag -> finance; sports tags win over a stray "world" tag (priority order).
    assert (
        classify_category(
            "NATO/EU troops in Ukraine?",
            tags=("russia", "politics", "geopolitics", "ukraine", "world", "nato"),
        )
        == "politics"
    )
    assert (
        classify_category("Kraken IPO in 2025?", tags=("crypto", "finance", "business", "ipos"))
        == "finance"
    )
    assert (
        classify_category("World Cup winner?", tags=("sports", "soccer", "fifa-world-cup", "world"))
        == "sports"
    )


def test_classify_category_keyword_fallback_geopolitics_to_politics() -> None:
    # Pool feed carries no tags, so keyword fallback must still route world affairs to politics.
    assert classify_category("Israel-Hamas ceasefire by December?") == "politics"
    assert classify_category("Will Ukraine and Russia sign a peace deal?") == "politics"
    assert classify_category("Venezuela military action in 2026?") == "politics"


def test_classify_category_ai_no_longer_matches_airspace() -> None:
    # Regression: the TECH " ai" fragment used to substring-match "airspace" and steal geopolitics
    # markets into Tech. The bare "ai" token now matches on word boundaries only.
    assert classify_category("Iran full airspace closure by August 31?") == "politics"
    assert classify_category("Will any AI model reach 1580 Coding Arena Score?") == "tech"

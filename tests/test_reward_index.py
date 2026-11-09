from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal

from polybot.config import Settings
from polybot.models import Book, Level
from polybot.rewards import index as ix
from polybot.rewards.index import (
    IndexedMarket,
    MarketQuery,
    assess_market,
    query_index,
)
from polybot.rewards.models import RewardPair
from polybot.rewards.scanner import ScanConfig
from polybot.rewards.selector import SelectorConfig

NOW = time.time()
SETTINGS = Settings(_env_file=None)


def _cfg() -> SelectorConfig:
    return SelectorConfig.from_settings(SETTINGS, size_shares=50.0)


def _scan_cfg() -> ScanConfig:
    return ScanConfig.from_settings(SETTINGS, size_shares=50.0, depth_based=True)


def _pair(
    *,
    question: str = "Will the high temp in Denver be 80-82F?",
    min_size: float = 50.0,
    max_spread: float = 0.045,
    rate: float = 150.0,
    one_day_change: float = 0.0,
    hours_to_end: float | None = 48.0,
) -> RewardPair:
    end = datetime.fromtimestamp(NOW + hours_to_end * 3600) if hours_to_end is not None else None
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
    )


def _book(asset_id: str, bids: dict[str, str], asks: dict[str, str]) -> Book:
    return Book(
        asset_id=asset_id,
        bids=[Level(price=Decimal(p), size=Decimal(s)) for p, s in bids.items()],
        asks=[Level(price=Decimal(p), size=Decimal(s)) for p, s in asks.items()],
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
    ).sorted()


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


def _assess(pair, ba, bb, *, category="weather", vol=(0.01, 0.005)) -> IndexedMarket:
    return assess_market(
        pair, ba, bb, category=category, vol=vol, now=NOW, cfg=_cfg(), scan_cfg=_scan_cfg()
    )


# --- assess_market: annotate, never drop ---


def test_calm_deep_market_is_safe_with_grade() -> None:
    pair = _pair()
    row = _assess(pair, _deep("yes-token"), _deep("no-token"))
    assert row.safe is True
    assert row.gate_reasons == []
    assert row.grade in {"A", "B"}
    assert row.enriched is True
    # size-independent: the row's shares is the market's own required_size, not any user size.
    assert row.min_shares == max(pair.rewards_min_size, pair.min_order_size)


def test_gap_risk_market_is_annotated_not_dropped() -> None:
    pair = _pair(question="Will Trump win the 2028 election?")
    row = _assess(pair, _deep("yes-token"), _deep("no-token"), category="politics")
    assert isinstance(row, IndexedMarket)  # returned, not None
    assert "gap_risk" in row.gate_reasons
    assert row.safe is False


def test_thin_book_flags_thin_and_unsafe() -> None:
    pair = _pair()
    row = _assess(pair, _thin("yes-token"), _thin("no-token"))
    assert "thin" in row.gate_reasons
    assert row.safe is False


def test_unenriched_row_has_only_cheap_gates() -> None:
    pair = _pair()
    row = _assess(pair, None, None)
    assert row.enriched is False
    assert row.vol_known is False
    # no book-derived gates without a book
    assert "thin" not in row.gate_reasons and "extreme_price" not in row.gate_reasons
    assert row.safe is True  # passes the cheap metadata gates


def test_unenriched_row_falls_back_to_metadata_price_for_display() -> None:
    import dataclasses

    pair = dataclasses.replace(_pair(), metadata_price_a=0.37)
    row = _assess(pair, None, None)
    assert row.enriched is False
    assert row.mid == 0.37  # display-only fallback from Gamma outcomePrices
    # a book mid still wins over metadata when the row is enriched
    enriched = _assess(pair, _deep("yes-token"), _deep("no-token"))
    assert enriched.mid == 0.50


def test_unenriched_row_without_metadata_price_keeps_mid_none() -> None:
    import dataclasses

    for bad in (None, 0.0, 1.0):
        pair = dataclasses.replace(_pair(), metadata_price_a=bad)
        row = _assess(pair, None, None)
        assert row.mid is None


def test_volatile_market_is_flagged() -> None:
    pair = _pair()
    row = _assess(pair, _deep("yes-token"), _deep("no-token"), vol=(0.20, 0.10))
    assert "volatile" in row.gate_reasons
    assert row.safe is False


# --- query_index: filter / sort / paginate ---


def _row(
    cid: str, q: str, cat: str, rate: float, minsh: float, grade: str, safe: bool,
    reasons=None, hours_left: float | None = 30.0,
):
    return IndexedMarket(
        condition_id=cid,
        question=q,
        category=cat,
        rate_per_day=rate,
        min_shares=minsh,
        max_spread=0.04,
        mid=0.5,
        grade=grade,
        safety={"A": 80.0, "B": 60.0, "C": 40.0}[grade],
        safe=safe,
        gate_reasons=reasons or [],
        risk_label="calm",
        sweep_lots=10.0,
        competitor_q=500.0,
        volatility_10m=0.01,
        volatility_2m=0.005,
        vol_known=True,
        hours_left=hours_left,
        one_day_change=0.0,
        volume_24hr=5000.0,
        enriched=True,
        token_a="ta" + cid,
        token_b="tb" + cid,
        outcome_a="Yes",
        outcome_b="No",
        tick_size="0.01",
        min_order_size=minsh,
    )


def _seed_rows() -> None:
    ix._index_cache["rows"] = [
        _row("1", "Will it rain in NYC", "weather", 120, 20, "A", True),
        _row("2", "BTC up or down", "finance", 300, 100, "B", True),
        _row("3", "Election winner", "politics", 500, 50, "C", False, ["gap_risk"]),
        _row("4", "Team A vs Team B live", "sports", 90, 20, "B", False, ["live_sports"]),
        _row("5", "Cheap weather low rate", "weather", 40, 20, "A", True),
    ]
    ix._index_cache["ts"] = 1.0


def test_query_safe_only_by_default_sorted_by_reward() -> None:
    _seed_rows()
    out = query_index(MarketQuery())
    assert [i["condition_id"] for i in out["items"]] == ["2", "1", "5"]
    assert out["total"] == 3


def test_query_include_risky_reveals_all() -> None:
    _seed_rows()
    out = query_index(MarketQuery(include_risky=True))
    assert out["total"] == 5
    assert [i["condition_id"] for i in out["items"]] == ["3", "2", "1", "4", "5"]


def test_query_sort_min_shares_ascending() -> None:
    _seed_rows()
    out = query_index(MarketQuery(sort="min_shares", descending=False))
    assert [i["min_shares"] for i in out["items"]] == [20, 20, 100]


def test_query_category_and_shares_filters() -> None:
    _seed_rows()
    weather = query_index(MarketQuery(categories=frozenset({"weather"})))
    assert {i["condition_id"] for i in weather["items"]} == {"1", "5"}
    big = query_index(MarketQuery(min_shares=50))
    assert [i["condition_id"] for i in big["items"]] == ["2"]


def test_query_min_hours_filters_out_near_resolution_markets() -> None:
    # Rows with varied runway; a no-end-date row (hours_left=None) has unbounded runway.
    ix._index_cache["rows"] = [
        _row("soon", "resolves in 10h", "weather", 100, 20, "A", True, hours_left=10.0),
        _row("fresh", "resolves in 40h", "weather", 100, 20, "A", True, hours_left=40.0),
        _row("far", "resolves in 100h", "weather", 100, 20, "A", True, hours_left=100.0),
        _row("none", "no end date", "weather", 100, 20, "A", True, hours_left=None),
    ]
    ix._index_cache["ts"] = 1.0

    # "> 1 day" (24h): drops the 10h market, keeps 40h/100h and the unbounded one.
    over_1d = query_index(MarketQuery(min_hours=24.0))
    assert {i["condition_id"] for i in over_1d["items"]} == {"fresh", "far", "none"}
    # "> 2 days" (48h): the freshly-opened ~40h weather market drops out too.
    over_2d = query_index(MarketQuery(min_hours=48.0))
    assert {i["condition_id"] for i in over_2d["items"]} == {"far", "none"}


def test_query_facets_ignore_category_filter() -> None:
    _seed_rows()
    out = query_index(MarketQuery(categories=frozenset({"weather"})))
    # facets count safe markets per category regardless of the active category filter
    assert out["facets"]["finance"] == 1
    assert out["facets"]["weather"] == 2


def test_query_search_and_pagination() -> None:
    _seed_rows()
    found = query_index(MarketQuery(search="btc", include_risky=True))
    assert [i["condition_id"] for i in found["items"]] == ["2"]
    page2 = query_index(MarketQuery(include_risky=True, page_size=2, page=2))
    assert [i["condition_id"] for i in page2["items"]] == ["1", "4"]
    assert page2["total"] == 5

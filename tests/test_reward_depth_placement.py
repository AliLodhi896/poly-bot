from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from polybot.config import Settings
from polybot.feed.books import BookStore, FastBook
from polybot.rewards.engine import ActiveRewardQuote, RewardMMEngine
from polybot.rewards.models import RewardPair
from polybot.rewards.planner import RewardQuotePlanner
from polybot.storage import Storage


def _pair(max_spread: float = 0.045, min_size: float = 20.0) -> RewardPair:
    return RewardPair(
        condition_id="0xabc",
        question="Will the depth placement work?",
        event_id="event-1",
        event_slug="reward-event",
        outcome_a="Yes",
        outcome_b="No",
        token_a="yes-token",
        token_b="no-token",
        volume_24hr=1234.0,
        market_spread=0.01,
        rewards_min_size=min_size,
        rewards_max_spread=max_spread,
        rewards_rate_per_day=10.0,
        tick_size=Decimal("0.01"),
        min_order_size=5.0,
        minimum_order_age_seconds=60,
        fee_rate=0.0,
        fees_enabled=False,
    )


def _book(bids: dict[str, str], asks: dict[str, str]) -> FastBook:
    return FastBook(
        token_id="yes-token",
        bids={Decimal(p): Decimal(s) for p, s in bids.items()},
        asks={Decimal(p): Decimal(s) for p, s in asks.items()},
        tick_size=Decimal("0.01"),
    )


def _depth_planner() -> RewardQuotePlanner:
    # Buffers come from the per-tier DEPTH_TIER_MULTS schedule keyed on the market's min_shares.
    return RewardQuotePlanner(0.5, depth_based=True)


def _plan(
    planner: RewardQuotePlanner,
    book: FastBook,
    *,
    pair: RewardPair | None = None,
    size: float = 20.0,
):
    return planner.plan_buy(
        pair=pair or _pair(),
        book=book,
        leg="a",
        outcome="Yes",
        size_shares=size,
    )


def test_thick_book_places_just_below_mid_at_target():
    # The discussed screenshot ladder. mid 0.385, zone floor 0.34, post-only ceiling 0.38.
    # min_shares 20 -> tier (30/35/45): target 700, floor 600. 0.38 has only 587.82 ahead (below
    # the 600 floor), so we step one tick deeper to 0.37 (1523 ahead) — the shallowest in-zone
    # tick that clears the floor.
    book = _book(
        bids={"0.38": "587.82", "0.37": "935.45", "0.36": "433.00", "0.35": "15.68"},
        asks={"0.39": "12.00", "0.40": "275.55", "0.41": "708.00"},
    )
    plan = _plan(_depth_planner(), book)
    assert plan is not None
    assert plan.price == pytest.approx(0.37)
    assert plan.locally_scoring


def test_thin_book_steps_deeper_to_clear_the_floor():
    # 120 shares per level. Floor 600 (min_shares 20 -> 30x) is first cleared only at 0.34
    # (cumulative 600), so we land at the zone edge — still in-zone, still protected.
    book = _book(
        bids={
            "0.38": "120",
            "0.37": "120",
            "0.36": "120",
            "0.35": "120",
            "0.34": "120",
        },
        asks={"0.39": "120"},
    )
    plan = _plan(_depth_planner(), book)
    assert plan is not None
    assert plan.price == pytest.approx(0.34)
    assert plan.within_spread


def test_too_thin_book_does_not_quote():
    # 50 shares per level: even the whole in-zone ladder (~250) cannot clear the 600 floor,
    # so the leg is not quoted at all (the market is too thin to farm at this size).
    book = _book(
        bids={
            "0.38": "50",
            "0.37": "50",
            "0.36": "50",
            "0.35": "50",
            "0.34": "50",
        },
        asks={"0.39": "50"},
    )
    assert _plan(_depth_planner(), book) is None


def test_overthick_best_bid_stays_at_back_of_book():
    # Best bid alone exceeds the ceil buffer (900). There is no shallower bid tick (0.39 is an
    # ask), so we cannot move closer to mid — we simply stand at the back of the best bid.
    book = _book(
        bids={"0.38": "2000", "0.37": "2000"},
        asks={"0.39": "50"},
    )
    plan = _plan(_depth_planner(), book)
    assert plan is not None
    assert plan.price == pytest.approx(0.38)


def test_higher_min_shares_market_needs_more_depth_and_sits_deeper():
    # The buffer keys to the market's min_shares, NOT our order size. Same book, two markets:
    # min_shares 20 (target 700, floor 600) sits at 0.38 (600 ahead, clears floor); min_shares 50
    # (target 850, floor 750) needs 0.37 (1200) since 0.38's 600 is below its floor.
    book = _book(
        bids={"0.38": "600", "0.37": "600", "0.36": "600", "0.35": "600", "0.34": "600"},
        asks={"0.39": "50"},
    )
    planner = _depth_planner()
    small_min = _plan(planner, book, pair=_pair(min_size=20.0))
    big_min = _plan(planner, book, pair=_pair(min_size=50.0))
    assert small_min is not None and small_min.price == pytest.approx(0.38)
    assert big_min is not None and big_min.price == pytest.approx(0.37)


def test_flag_off_keeps_legacy_fraction_placement():
    # Identical thin book; with depth mode OFF the planner uses the fixed 0.5 fraction:
    # mid 0.385 - 0.0225 = 0.3625 -> 0.36, capped at best bid. Proves the legacy path is intact.
    book = _book(
        bids={
            "0.38": "100",
            "0.37": "100",
            "0.36": "100",
            "0.35": "100",
            "0.34": "100",
        },
        asks={"0.39": "100"},
    )
    legacy = RewardQuotePlanner(0.5)  # depth_based defaults to False
    plan = legacy.plan_buy(
        pair=_pair(), book=book, leg="a", outcome="Yes", size_shares=20.0
    )
    assert plan is not None
    assert plan.price == pytest.approx(0.36)


def _engine(tmp_path, pair: RewardPair, book: FastBook, *, depth: bool = True):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "reward.sqlite3",
        reward_mm_snapshot_interval_seconds=0.0,
        reward_mm_depth_based_placement=depth,
    )
    storage = Storage(settings.database_path)
    storage.init()
    engine = RewardMMEngine(settings, storage)
    engine.run_id = storage.create_reward_run(1, 20, {"strategy": "depth_test"})
    store = BookStore({pair.token_a: pair.condition_id})
    store.books[pair.token_a] = book
    engine.books = store
    engine.pairs[pair.condition_id] = pair
    engine.pair_cache[pair.condition_id] = pair
    return engine


def _active(plan) -> ActiveRewardQuote:
    return ActiveRewardQuote(
        quote_uid="q1", plan=plan, opened_at=10.0, queue_ahead=0.0, last_eval_at=10.0
    )


def test_depth_hold_keeps_quote_while_in_band(tmp_path):
    # Planner places at 0.37 (700 ahead, nearest target 700). Depth-ahead stays inside the
    # [600, 900] band, so the hold keeps the resting price instead of re-targeting each tick.
    pair = _pair()
    book = _book(bids={"0.38": "100", "0.37": "600", "0.36": "600"}, asks={"0.39": "100"})
    plan = _plan(_depth_planner(), book)
    assert plan is not None and plan.price == pytest.approx(0.37)
    engine = _engine(tmp_path, pair, book)
    held = engine._depth_hold_plan(pair, _active(plan), plan, now=20.0)
    assert held is not None
    assert held.price == pytest.approx(0.37)


def test_depth_hold_reprices_when_book_thins(tmp_path):
    # Same resting 0.37 quote, but the book ahead collapses below the 600 floor → release the
    # hold so the normal reprice can step the quote deeper.
    pair = _pair()
    place_book = _book(bids={"0.38": "100", "0.37": "600"}, asks={"0.39": "100"})
    plan = _plan(_depth_planner(), place_book)
    engine = _engine(tmp_path, pair, place_book)
    engine.books.books[pair.token_a] = _book(
        bids={"0.38": "50", "0.37": "50"}, asks={"0.39": "50"}
    )
    assert engine._depth_hold_plan(pair, _active(plan), plan, now=20.0) is None


def test_depth_hold_reprices_when_book_thickens_past_ceil(tmp_path):
    # Depth ahead swells past the 900 ceil AND the shallower tick the planner re-targets (0.38,
    # holding 5000 >= target) is robustly in-band → release the hold so we move back toward mid.
    pair = _pair()
    place_book = _book(bids={"0.38": "100", "0.37": "600"}, asks={"0.39": "100"})
    plan = _plan(_depth_planner(), place_book)  # resting at 0.37
    engine = _engine(tmp_path, pair, place_book)
    thick = _book(bids={"0.38": "5000", "0.37": "5000"}, asks={"0.39": "100"})
    engine.books.books[pair.token_a] = thick
    new = _plan(_depth_planner(), thick)  # planner re-targets 0.38 on the thick book
    assert engine._depth_hold_plan(pair, _active(plan), new, now=20.0) is None


def test_depth_hold_is_noop_when_flag_off(tmp_path):
    # With the flag OFF the hold never engages, so legacy reprice behaviour is untouched.
    pair = _pair()
    book = _book(bids={"0.38": "100", "0.37": "600", "0.36": "600"}, asks={"0.39": "100"})
    plan = _plan(_depth_planner(), book)
    engine = _engine(tmp_path, pair, book, depth=False)
    assert engine._depth_hold_plan(pair, _active(plan), plan, now=20.0) is None


def _hold_result(tmp_path, book, *, old_price, new_price, size=50.0):
    # Build a resting quote at ``old_price`` and a planner target at ``new_price`` on ``book``,
    # then ask the hold logic whether to keep the resting price. min_shares 20 -> floor 600,
    # target 700, ceil 900.
    pair = _pair()
    engine = _engine(tmp_path, pair, book)
    base = _plan(_depth_planner(), book, size=size)
    assert base is not None
    new = replace(base, price=new_price)
    old = replace(base, price=old_price)
    return engine._depth_hold_plan(pair, _active(old), new, now=20.0)


def test_depth_hold_stays_deep_when_only_marginal_shallower_tick(tmp_path):
    # The oscillation trap: 0.42 is over-protected (>900) and the shallower 0.43 is only
    # MARGINALLY protected (650, in [floor, target)). The old symmetric band would flip us up to
    # 0.43; the new logic holds 0.42 because moving up is not a robust improvement.
    book = _book(
        bids={"0.44": "300", "0.43": "350", "0.42": "550"}, asks={"0.46": "100"}
    )
    held = _hold_result(tmp_path, book, old_price=0.42, new_price=0.43)
    assert held is not None
    assert held.price == pytest.approx(0.42)


def test_depth_hold_releases_up_when_shallower_tick_robustly_in_band(tmp_path):
    # Same shape but 0.43 now holds 800 (>= target 700) — a genuine, stable improvement — so the
    # hold releases and lets the reprice move us up toward mid.
    book = _book(
        bids={"0.44": "400", "0.43": "400", "0.42": "500"}, asks={"0.46": "100"}
    )
    assert _hold_result(tmp_path, book, old_price=0.42, new_price=0.43) is None


def test_depth_hold_releases_deeper_below_floor(tmp_path):
    # Depth ahead of 0.43 collapses below the 600 floor — safety release regardless of the band.
    book = _book(
        bids={"0.44": "100", "0.43": "100", "0.42": "800"}, asks={"0.46": "100"}
    )
    assert _hold_result(tmp_path, book, old_price=0.43, new_price=0.42) is None


def test_depth_hold_keeps_price_while_in_band(tmp_path):
    # 0.42 sits inside [600, 900] (own-size-netted depth 650) — hold even though the planner
    # would nominally re-target a different tick.
    book = _book(
        bids={"0.44": "100", "0.43": "250", "0.42": "350"}, asks={"0.46": "100"}
    )
    held = _hold_result(tmp_path, book, old_price=0.42, new_price=0.43)
    assert held is not None
    assert held.price == pytest.approx(0.42)


def test_snapshot_records_actual_depth_ahead(tmp_path):
    # The snapshot must log the real cumulative depth ahead of the resting order (own size
    # netted) so a fill can be diagnosed afterwards. Book bids above 0.44 = 600, our level 0.44
    # holds 400 (minus our own 50) → 950 ahead.
    pair = _pair()
    book = _book(
        bids={"0.47": "100", "0.46": "200", "0.45": "300", "0.44": "400"},
        asks={"0.48": "50"},
    )
    engine = _engine(tmp_path, pair, book)
    plan = replace(_plan(_depth_planner(), book, size=50), price=0.44)
    snap = engine._quote_snapshot(_active(plan), pair, now=100.0)
    assert snap["depth_ahead"] == pytest.approx(950.0)


def _filter_engine(tmp_path, *, depth: bool):
    # Disable the unrelated top-depth and extreme-midpoint gates so the depth-for-size filter
    # is the only thing that can reject a market in these tests.
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "filter.sqlite3",
        reward_mm_depth_based_placement=depth,
        reward_mm_min_top_depth_shares=0.0,
        reward_mm_min_midpoint=0.0,
    )
    storage = Storage(settings.database_path)
    storage.init()
    return RewardMMEngine(settings, storage)


def test_prefilter_passes_when_both_legs_clear_the_floor(tmp_path):
    pair = _pair()
    thick = _book(bids={"0.38": "600", "0.37": "600"}, asks={"0.39": "600"})
    engine = _filter_engine(tmp_path, depth=True)
    assert engine._market_passes_filters(pair, thick, thick, 20.0) == (True, "")


def test_prefilter_rejects_market_too_thin_for_size(tmp_path):
    pair = _pair()
    thick = _book(bids={"0.38": "600", "0.37": "600"}, asks={"0.39": "600"})
    thin = _book(
        bids={"0.38": "50", "0.37": "50", "0.36": "50", "0.35": "50", "0.34": "50"},
        asks={"0.39": "50"},
    )
    engine = _filter_engine(tmp_path, depth=True)
    # One thin leg is enough to disqualify the market for our size.
    assert engine._market_passes_filters(pair, thick, thin, 20.0) == (
        False,
        "thin_for_size",
    )


def test_prefilter_higher_min_shares_is_stricter(tmp_path):
    # The floor keys to the market's min_shares. Same book (in-zone ladder tops out at 700):
    # min_shares 20 -> floor 600 (farmable), min_shares 50 -> floor 750 (too thin -> rejected).
    book = _book(
        bids={"0.38": "140", "0.37": "140", "0.36": "140", "0.35": "140", "0.34": "140"},
        asks={"0.39": "140"},
    )
    engine = _filter_engine(tmp_path, depth=True)
    assert engine._market_passes_filters(_pair(min_size=20.0), book, book, 20.0) == (
        True,
        "",
    )
    assert engine._market_passes_filters(_pair(min_size=50.0), book, book, 50.0) == (
        False,
        "thin_for_size",
    )


def test_prefilter_inactive_when_flag_off(tmp_path):
    pair = _pair()
    thin = _book(bids={"0.38": "50"}, asks={"0.39": "50"})
    engine = _filter_engine(tmp_path, depth=False)
    # Depth filter not applied and other gates disabled → the thin market passes.
    assert engine._market_passes_filters(pair, thin, thin, 20.0) == (True, "")

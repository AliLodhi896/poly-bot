from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from polybot.api.clob import ClobClient, ClobMarketInfo, MarketFeeInfo
from polybot.config import Settings
from polybot.feed.books import BookStore, FastBook, TradePrint
from polybot.models import Book, Market, RewardConfig, RewardRate, Token
from polybot.rewards.engine import RewardMMEngine
from polybot.rewards.models import RewardPair, normalize_reward_spread
from polybot.rewards.planner import RewardQuotePlanner
from polybot.storage import Storage


def reward_pair(
    *,
    min_size: float = 100.0,
    max_spread: float = 0.03,
    fee_rate: float = 0.0,
    fees_enabled: bool = False,
) -> RewardPair:
    return RewardPair(
        condition_id="0xabc",
        question="Will reward MM work?",
        event_id="event-1",
        event_slug="reward-event",
        outcome_a="Yes",
        outcome_b="No",
        token_a="yes-token",
        token_b="no-token",
        volume_24hr=1234.0,
        market_spread=0.02,
        rewards_min_size=min_size,
        rewards_max_spread=max_spread,
        rewards_rate_per_day=10.0,
        tick_size=Decimal("0.01"),
        min_order_size=5.0,
        minimum_order_age_seconds=60,
        fee_rate=fee_rate,
        fees_enabled=fees_enabled,
    )


def alternate_reward_pair(base: RewardPair, condition_id: str = "0xdef") -> RewardPair:
    return replace(
        base,
        condition_id=condition_id,
        question="Will stale reward MM work?",
        token_a=f"{condition_id}-yes-token",
        token_b=f"{condition_id}-no-token",
    )


def reward_books(
    *,
    pair: RewardPair | None = None,
    yes_bid: str = "0.55",
    yes_ask: str = "0.57",
    no_bid: str = "0.43",
    no_ask: str = "0.45",
) -> BookStore:
    pair = pair or reward_pair()
    books = BookStore({pair.token_a: pair.condition_id, pair.token_b: pair.condition_id})
    books.books[pair.token_a] = FastBook(
        pair.token_a,
        bids=_ladder(yes_bid, step=-1),
        asks={Decimal(yes_ask): Decimal("1000")},
        tick_size=Decimal("0.01"),
    )
    books.books[pair.token_b] = FastBook(
        pair.token_b,
        bids=_ladder(no_bid, step=-1),
        asks={Decimal(no_ask): Decimal("1000")},
        tick_size=Decimal("0.01"),
    )
    return books


def _ladder(top: str, *, step: int, levels: int = 4, size: str = "1000") -> dict:
    """A multi-level side, so depth-based placement (the shipped default) can find a tick.

    A single 1000-share level is thinner than any real reward market and the planner
    correctly declines to quote it — see test_reward_depth_placement.py. The top price is
    unchanged, so best-bid/ask assertions elsewhere still hold.
    """
    tick = Decimal("0.01")
    return {
        Decimal(top) + tick * step * i: Decimal(size) for i in range(levels)
    }


def reward_engine(
    tmp_path,
    *,
    pair: RewardPair | None = None,
    books: BookStore | None = None,
    max_position_shares: float = 500.0,
    max_exposure_usdc: float = 1000.0,
) -> RewardMMEngine:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "reward.sqlite3",
        reward_mm_snapshot_interval_seconds=0.0,
        reward_mm_max_loss_usdc=1.0,
        reward_mm_max_position_shares=max_position_shares,
        reward_mm_max_exposure_usdc=max_exposure_usdc,
    )
    storage = Storage(settings.database_path)
    storage.init()
    engine = RewardMMEngine(settings, storage)
    engine.run_id = storage.create_reward_run(
        1, 100, {"strategy": "reward_mm_test", "max_loss_usdc": engine.max_loss_usdc}
    )
    engine.books = books or reward_books()
    selected = pair or reward_pair()
    engine.pairs[selected.condition_id] = selected
    engine.selected_pairs[selected.condition_id] = selected
    engine.pair_cache[selected.condition_id] = selected
    engine.max_position_shares = settings.reward_mm_max_position_shares
    engine.max_exposure_usdc = settings.reward_mm_max_exposure_usdc
    return engine


def yes_buy_plan(pair: RewardPair, books: BookStore, size: float = 100.0):
    plan = RewardQuotePlanner(quote_distance_fraction=0.5).plan_buy(
        pair=pair,
        book=books.books[pair.token_a],
        leg="a",
        outcome=pair.outcome_a,
        size_shares=size,
    )
    assert plan is not None
    return plan


def open_reward_quote(engine: RewardMMEngine, pair: RewardPair, plan, now: float = 10.0):
    active = engine._open_quote(pair, plan, now)
    active.queue_ahead = 0.0
    engine.active_quotes[plan.key()] = active
    return active


def test_reward_spread_normalizes_cents_to_probability_units() -> None:
    assert normalize_reward_spread(2.5) == 0.025
    assert normalize_reward_spread(0.025) == 0.025
    assert normalize_reward_spread(None) is None


def test_planner_places_buy_quotes_inside_reward_zone_and_post_only() -> None:
    plans = RewardQuotePlanner(quote_distance_fraction=0.5).plan_pair(
        reward_pair(), reward_books(), 100
    )

    yes = next(plan for plan in plans if plan.outcome == "Yes")
    no = next(plan for plan in plans if plan.outcome == "No")

    assert yes.price == 0.54
    assert yes.midpoint == 0.56
    assert yes.distance_from_midpoint <= yes.max_spread
    assert yes.post_only_safe is True
    assert yes.locally_scoring is True
    assert no.price == 0.42
    assert no.midpoint == 0.44
    assert no.locally_scoring is True


def _single_book(token: str, bid: str, ask: str, tick: str = "0.01") -> FastBook:
    return FastBook(
        token,
        bids={Decimal(bid): Decimal("1000")},
        asks={Decimal(ask): Decimal("1000")},
        tick_size=Decimal(tick),
    )


def test_planner_buy_joins_best_bid_instead_of_improving_it() -> None:
    # Wide book, small target distance: the distance-only price (0.54) would jump 4 ticks in
    # FRONT of the resting bid (0.50). Queue-join caps the quote AT best_bid so we rest behind
    # the existing queue, while still inside the reward zone (distance 0.05 <= max_spread 0.10).
    pair = reward_pair(max_spread=0.10)
    book = _single_book(pair.token_a, bid="0.50", ask="0.60")
    plan = RewardQuotePlanner(quote_distance_fraction=0.1).plan_buy(
        pair=pair, book=book, leg="a", outcome=pair.outcome_a, size_shares=100
    )
    assert plan is not None
    assert plan.price == 0.50  # == best_bid, not 0.54
    assert plan.distance_from_midpoint <= plan.max_spread + 1e-12
    assert plan.post_only_safe is True


def test_planner_buy_stays_deeper_than_bid_when_book_is_tight() -> None:
    # Tight book where the distance target (mid 0.555 - 0.015 = 0.54) is already below the
    # best bid (0.55): queue-join is a no-op and the quote keeps its deeper, safer price.
    pair = reward_pair(max_spread=0.03)
    book = _single_book(pair.token_a, bid="0.55", ask="0.56")
    plan = RewardQuotePlanner(quote_distance_fraction=0.5).plan_buy(
        pair=pair, book=book, leg="a", outcome=pair.outcome_a, size_shares=100
    )
    assert plan is not None
    assert plan.price == 0.54
    assert plan.price < 0.55  # strictly deeper than best_bid


def test_planner_buy_improves_to_zone_edge_when_book_wider_than_reward_zone() -> None:
    # Book far wider than the reward zone: best_bid (0.40) sits below lower_bound
    # (mid 0.50 - max_spread 0.03 = 0.47). To score at all we must lift to the zone edge —
    # the single case where improving the bid is allowed.
    pair = reward_pair(max_spread=0.03)
    book = _single_book(pair.token_a, bid="0.40", ask="0.60")
    plan = RewardQuotePlanner(quote_distance_fraction=0.5).plan_buy(
        pair=pair, book=book, leg="a", outcome=pair.outcome_a, size_shares=100
    )
    assert plan is not None
    assert plan.price == pytest.approx(0.47)  # lower_bound edge, above best_bid
    assert plan.within_spread is True
    assert plan.post_only_safe is True


def test_planner_places_passive_sell_exit_inside_reward_zone() -> None:
    pair = reward_pair()
    books = reward_books()

    plan = RewardQuotePlanner(quote_distance_fraction=0.5).plan_sell(
        pair=pair,
        book=books.books[pair.token_a],
        leg="a",
        outcome=pair.outcome_a,
        size_shares=100,
        minimum_price=0.54,
    )

    assert plan is not None
    assert plan.side == "SELL"
    assert plan.price == 0.56
    assert plan.post_only_safe is True
    assert plan.locally_scoring is True


def test_planner_marks_quotes_below_reward_min_size_as_non_scoring() -> None:
    plans = RewardQuotePlanner(quote_distance_fraction=0.5).plan_pair(
        reward_pair(min_size=250), reward_books(), 100
    )

    assert len(plans) == 2
    assert all(not plan.locally_scoring for plan in plans)
    assert {plan.reason for plan in plans} == {"below_min_size"}


def test_buy_fill_opens_inventory_and_blocks_same_token_rebuy(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(no_bid="0.48", no_ask="0.50")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 10.0
    plan = yes_buy_plan(pair, books)
    open_reward_quote(engine, pair, plan)

    engine._apply_trade(
        TradePrint(
            token_id=pair.token_a,
            price=Decimal("0.54"),
            size=Decimal("100"),
            side="SELL",
            timestamp_ms=None,
        ),
        now=11.0,
    )
    engine._handle_pair(pair, 100, force_snapshot=True)

    item = engine.inventory[(pair.condition_id, pair.token_a)]
    active = {(quote.plan.token_id, quote.plan.side) for quote in engine.active_quotes.values()}
    assert item.shares == 100
    assert item.avg_price == pytest.approx(0.54)
    assert (pair.token_a, "BUY") not in active
    assert (pair.token_a, "SELL") in active
    assert (pair.token_b, "BUY") in active


def test_reward_adjusted_near_flat_hedge_closes_inventory(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(yes_bid="0.56", yes_ask="0.58", no_bid="0.43", no_ask="0.445")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 10.0
    plan = yes_buy_plan(pair, books)
    engine._add_inventory(plan, shares=100, price=0.56, now=10.0, quote_uid=None)
    item = engine.inventory[(pair.condition_id, pair.token_a)]
    item.reward_credit_usdc = 1.0

    engine._manage_inventory(pair, now=11.0)

    assert item.shares == 0
    assert item.close_mode == "hedge_locked"
    assert item.closed_pnl == pytest.approx(-0.5)
    assert item.reward_adjusted_pnl == pytest.approx(0.5)
    assert engine.metrics.locked_bundles == 1


def test_passive_sell_fill_closes_inventory(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(no_bid="0.48", no_ask="0.50")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 10.0
    plan = yes_buy_plan(pair, books)
    engine._add_inventory(plan, shares=100, price=0.54, now=10.0, quote_uid=None)
    engine._handle_pair(pair, 100, force_snapshot=True)
    active_exit = next(
        quote
        for quote in engine.active_quotes.values()
        if quote.plan.token_id == pair.token_a and quote.plan.side == "SELL"
    )
    active_exit.queue_ahead = 0.0

    engine._apply_trade(
        TradePrint(
            token_id=pair.token_a,
            price=Decimal(str(active_exit.plan.price)),
            size=Decimal("100"),
            side="BUY",
            timestamp_ms=None,
        ),
        now=12.0,
    )

    item = engine.inventory[(pair.condition_id, pair.token_a)]
    assert item.shares == 0
    assert item.close_mode == "passive_exit"
    assert item.closed_pnl == pytest.approx((active_exit.plan.price - 0.54) * 100)
    assert engine.metrics.passive_exits == 1


def test_emergency_exit_chooses_best_route_when_max_loss_is_reached(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(yes_bid="0.50", yes_ask="0.52", no_bid="0.46", no_ask="0.48")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 1.0
    plan = yes_buy_plan(pair, books)
    engine._add_inventory(plan, shares=100, price=0.56, now=10.0, quote_uid=None)

    engine._manage_inventory(pair, now=11.0)

    item = engine.inventory[(pair.condition_id, pair.token_a)]
    assert item.shares == 0
    assert item.close_mode == "emergency_exit"
    assert item.closed_pnl == pytest.approx(-4.0)
    assert engine.metrics.emergency_exits == 1
    assert engine.metrics.stop_slippage_usdc == pytest.approx(-3.0)

    with engine.storage.connect() as db:
        closure = db.execute(
            "SELECT * FROM reward_inventory_closures WHERE run_id=?", (engine.run_id,)
        ).fetchone()
    assert closure["risk_reason"] == "liquidation_stop"
    assert closure["exit_route"] == "buy_opposite"
    assert closure["trigger_pnl"] == pytest.approx(-6.0)
    assert closure["liquidation_pnl_at_trigger"] == pytest.approx(-6.0)
    assert closure["hedge_pnl_at_trigger"] == pytest.approx(-4.0)


def test_hedge_pnl_can_trigger_emergency_before_liquidation_loss(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(yes_bid="0.57", yes_ask="0.59", no_bid="0.38", no_ask="0.60")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 10.0
    plan = yes_buy_plan(pair, books)
    engine._add_inventory(plan, shares=100, price=0.58, now=10.0, quote_uid=None)

    engine._manage_inventory(pair, now=11.0)

    item = engine.inventory[(pair.condition_id, pair.token_a)]
    assert item.shares == 0
    assert item.close_mode == "emergency_exit"
    assert item.closed_pnl == pytest.approx(-1.0)
    assert engine.metrics.emergency_exits == 1
    assert engine.metrics.stop_slippage_usdc == 0

    with engine.storage.connect() as db:
        closure = db.execute(
            "SELECT * FROM reward_inventory_closures WHERE run_id=?", (engine.run_id,)
        ).fetchone()
    assert closure["risk_reason"] == "hedge_stop"
    assert closure["exit_route"] == "sell_current"
    assert closure["trigger_pnl"] == pytest.approx(-18.0)
    assert closure["exit_pnl"] == pytest.approx(-1.0)
    assert closure["planned_max_loss_usdc"] == pytest.approx(10.0)


class _FakeLive:
    def __init__(self, taker_filled: bool = True) -> None:
        self.taker_filled = taker_filled
        self.taker_calls: list[str] = []
        self.alert_calls: list[tuple[str, float | None]] = []

    def execute_taker_exit(self, pair, item, route) -> bool:
        self.taker_calls.append(route.route)
        return self.taker_filled

    def alert_emergency(self, item, reason, trigger_pnl) -> None:
        self.alert_calls.append((reason, trigger_pnl))


def _stopping_engine(tmp_path):
    pair = reward_pair()
    books = reward_books(yes_bid="0.50", yes_ask="0.52", no_bid="0.46", no_ask="0.48")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 1.0
    engine._add_inventory(
        yes_buy_plan(pair, books), shares=100, price=0.56, now=10.0, quote_uid=None
    )
    return engine, pair


def test_live_small_stop_auto_cuts_with_taker(tmp_path) -> None:
    engine, pair = _stopping_engine(tmp_path)
    engine.settings.reward_mm_live_auto_exit_max_shares = 200.0
    live = _FakeLive(taker_filled=True)
    engine.live = live

    engine._manage_inventory(pair, now=11.0)

    item = engine.inventory[(pair.condition_id, pair.token_a)]
    assert item.shares == 0
    assert item.close_mode == "auto_taker_exit"
    assert live.taker_calls == ["buy_opposite"]  # cheaper route here (-4 vs -6)
    assert live.alert_calls == []
    assert engine.metrics.auto_taker_exits == 1


def test_live_large_stop_alerts_instead_of_auto_cut(tmp_path) -> None:
    engine, pair = _stopping_engine(tmp_path)
    engine.settings.reward_mm_live_auto_exit_max_shares = 50.0  # 100-share position exceeds cap
    live = _FakeLive(taker_filled=True)
    engine.live = live

    engine._manage_inventory(pair, now=11.0)

    item = engine.inventory[(pair.condition_id, pair.token_a)]
    assert item.shares == 100  # held, not auto-cut
    assert live.taker_calls == []
    assert len(live.alert_calls) == 1
    assert engine.metrics.auto_taker_exits == 0


def test_live_auto_cut_falls_back_to_alert_when_fok_killed(tmp_path) -> None:
    engine, pair = _stopping_engine(tmp_path)
    engine.settings.reward_mm_live_auto_exit_max_shares = 200.0
    live = _FakeLive(taker_filled=False)  # FOK killed
    engine.live = live

    engine._manage_inventory(pair, now=11.0)

    item = engine.inventory[(pair.condition_id, pair.token_a)]
    assert item.shares == 100  # position kept open after the kill
    assert live.taker_calls == ["buy_opposite"]
    assert len(live.alert_calls) == 1
    assert engine.metrics.auto_taker_exits == 0


def test_stop_action_throttled_within_window(tmp_path) -> None:
    # A just-filled position whose shares are not settled makes the FOK fail and the stop re-fire
    # every tick; the throttle limits retries/alerts to the configured cadence.
    engine, pair = _stopping_engine(tmp_path)
    engine.settings.reward_mm_live_auto_exit_max_shares = 200.0
    engine.settings.reward_mm_stop_action_throttle_seconds = 3.0
    live = _FakeLive(taker_filled=False)  # FOK keeps failing → position stays open
    engine.live = live

    engine._manage_inventory(pair, now=100.0)
    engine._manage_inventory(pair, now=101.0)  # within 3s → suppressed
    assert len(live.taker_calls) == 1
    assert len(live.alert_calls) == 1

    engine._manage_inventory(pair, now=104.0)  # past throttle → retried
    assert len(live.taker_calls) == 2


def test_live_auto_exit_disabled_by_default(tmp_path) -> None:
    engine, pair = _stopping_engine(tmp_path)
    live = _FakeLive(taker_filled=True)  # max_shares default 0 → auto-exit off
    engine.live = live

    engine._manage_inventory(pair, now=11.0)

    assert live.taker_calls == []
    assert len(live.alert_calls) == 1


def test_fill_retires_market_from_new_entries_but_keeps_managing(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(yes_bid="0.59", yes_ask="0.61", no_bid="0.42", no_ask="0.44")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 10.0

    engine._add_inventory(
        yes_buy_plan(pair, books), shares=100, price=0.60, now=10.0, quote_uid=None
    )

    assert pair.condition_id in engine.markets_retired_after_fill
    # No new entries on this market...
    assert engine._can_plan_entry_or_hedge(pair, now=11.0) is False
    # ...but the open leg is still managed (the race rests its sell + completion).
    exit_plans = engine._manage_inventory(pair, now=11.0)
    assert any(p.token_id == pair.token_a and p.side == "SELL" for p in exit_plans)


def test_retired_market_blocks_entries_after_position_closes(tmp_path) -> None:
    pair = reward_pair()
    engine = reward_engine(tmp_path, pair=pair)
    engine.markets_retired_after_fill.add(pair.condition_id)

    # Even with no open inventory (position already resolved), entries stay paused until restart.
    assert engine._open_inventory_for_pair(pair.condition_id) == []
    assert engine._can_plan_entry_or_hedge(pair, now=11.0) is False


def test_fill_does_not_retire_other_markets(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    other = alternate_reward_pair(pair)
    engine.pairs[other.condition_id] = other
    engine.selected_pairs[other.condition_id] = other
    engine.pair_cache[other.condition_id] = other

    engine._add_inventory(
        yes_buy_plan(pair, books), shares=100, price=0.54, now=10.0, quote_uid=None
    )

    assert other.condition_id not in engine.markets_retired_after_fill
    assert engine._can_plan_entry_or_hedge(other, now=11.0) is True


def test_retire_after_fill_can_be_disabled(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books()
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.settings.reward_mm_retire_market_after_fill = False

    engine._add_inventory(
        yes_buy_plan(pair, books), shares=100, price=0.54, now=10.0, quote_uid=None
    )

    assert pair.condition_id not in engine.markets_retired_after_fill
    assert engine._can_plan_entry_or_hedge(pair, now=11.0) is True


def test_new_fill_after_full_close_creates_fresh_inventory(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books()
    engine = reward_engine(tmp_path, pair=pair, books=books)
    plan = yes_buy_plan(pair, books)
    engine._add_inventory(plan, shares=100, price=0.54, now=10.0, quote_uid=None)
    item = engine.inventory[(pair.condition_id, pair.token_a)]
    first_uid = item.inventory_uid

    engine._close_inventory(
        item,
        close_mode="hedge_locked",
        realized_pnl=1.0,
        now=11.0,
        hedge_price=0.45,
        bundle_cost=0.99,
    )
    engine._add_inventory(plan, shares=50, price=0.50, now=12.0, quote_uid=None)

    reopened = engine.inventory[(pair.condition_id, pair.token_a)]
    assert reopened.inventory_uid != first_uid
    assert reopened.closed_at is None
    assert reopened.close_mode is None
    assert reopened.state == "open_inventory"
    assert reopened.shares == 50
    assert reopened.avg_price == pytest.approx(0.50)


def test_max_position_caps_new_entry_quote_size(tmp_path) -> None:
    pair = reward_pair(min_size=100)
    books = reward_books()
    engine = reward_engine(tmp_path, pair=pair, books=books, max_position_shares=150)
    plans = engine.planner.plan_pair(pair, books, 250)

    filtered = engine._filter_entry_plans_for_inventory(pair, plans)

    assert len(filtered) == 2
    assert {plan.size for plan in filtered} == {150}
    assert all(plan.locally_scoring for plan in filtered)


def test_exposure_cap_blocks_new_entries_but_allows_hedge_quote(tmp_path) -> None:
    # A tiny exposure cap blocks fresh entries; once a leg is held, the entry path pauses
    # entirely (the race in _manage_inventory owns both legs) and the completion buy is still
    # emitted there, never suppressed by the entry exposure cap.
    pair = reward_pair(min_size=100)
    # Book where the immediate taker arb does NOT fire (set cost 1.04 > ceiling) so the position
    # is managed by the race rather than instantly bundle-locked.
    books = reward_books(yes_bid="0.59", yes_ask="0.61", no_bid="0.42", no_ask="0.44")
    engine = reward_engine(tmp_path, pair=pair, books=books, max_exposure_usdc=10)
    engine.max_loss_usdc = 10.0
    plans = engine.planner.plan_pair(pair, books, 250)

    assert engine._filter_entry_plans_for_inventory(pair, plans) == []

    engine._add_inventory(
        yes_buy_plan(pair, books), shares=100, price=0.60, now=10.0, quote_uid=None
    )
    assert engine._filter_entry_plans_for_inventory(pair, plans) == []

    exit_plans = engine._manage_inventory(pair, now=11.0)
    completion = [p for p in exit_plans if p.token_id == pair.token_b and p.side == "BUY"]
    assert len(completion) == 1
    assert completion[0].size == 100


def test_exposure_cap_accounts_for_new_quotes_as_a_batch(tmp_path) -> None:
    pair = reward_pair(min_size=100)
    books = reward_books()
    engine = reward_engine(tmp_path, pair=pair, books=books, max_exposure_usdc=80)
    plans = engine.planner.plan_pair(pair, books, 100)

    filtered = engine._filter_entry_plans_for_inventory(pair, plans)

    assert len(filtered) == 1
    assert filtered[0].token_id == pair.token_a
    assert filtered[0].size == 100


def test_exposure_cap_is_per_market_not_global(tmp_path) -> None:
    # A fill (open inventory) on market A must NOT shrink market B's entry budget: the exposure
    # cap is applied PER-MARKET, so B keeps quoting BOTH legs. Regression for the bug where one
    # fill collapsed every other market to a single directional leg.
    pair_a = reward_pair()
    pair_b = alternate_reward_pair(pair_a)
    books = BookStore(
        {
            pair_a.token_a: pair_a.condition_id,
            pair_a.token_b: pair_a.condition_id,
            pair_b.token_a: pair_b.condition_id,
            pair_b.token_b: pair_b.condition_id,
        }
    )
    books.books.update(reward_books(pair=pair_a).books)
    books.books.update(reward_books(pair=pair_b).books)

    # $120 cap fits both legs of one market ($96) but not that plus a $60 fill on another market —
    # so a GLOBAL cap would drop B's second leg, while a per-market cap keeps it.
    engine = reward_engine(tmp_path, pair=pair_a, books=books, max_exposure_usdc=120)
    engine.max_loss_usdc = 10.0
    engine.pairs[pair_b.condition_id] = pair_b
    engine.selected_pairs[pair_b.condition_id] = pair_b
    engine.pair_cache[pair_b.condition_id] = pair_b

    plans_b = engine.planner.plan_pair(pair_b, books, 100)
    assert len(plans_b) == 2
    # Baseline: with no inventory anywhere, B quotes both legs within the cap.
    assert len(engine._filter_entry_plans_for_inventory(pair_b, plans_b)) == 2

    # A fill on market A opens a $60 leg (100 shares @ 0.60).
    engine._add_inventory(
        yes_buy_plan(pair_a, books), shares=100, price=0.60, now=10.0, quote_uid=None
    )

    # Market A pauses its own entries (its open leg is managed by the race)...
    assert (
        engine._filter_entry_plans_for_inventory(
            pair_a, engine.planner.plan_pair(pair_a, books, 100)
        )
        == []
    )
    # ...but market B is untouched and still quotes BOTH legs.
    filtered_b = engine._filter_entry_plans_for_inventory(pair_b, plans_b)
    assert {plan.outcome for plan in filtered_b} == {"Yes", "No"}


def test_universe_refresh_cancels_stale_quotes_without_inventory(tmp_path) -> None:
    pair = reward_pair()
    stale_pair = alternate_reward_pair(pair)
    stale_books = reward_books(pair=stale_pair)
    engine = reward_engine(tmp_path, pair=pair)
    stale_plan = yes_buy_plan(stale_pair, stale_books)
    active = open_reward_quote(engine, stale_pair, stale_plan)
    engine.pairs[stale_pair.condition_id] = stale_pair
    engine.pair_cache[stale_pair.condition_id] = stale_pair

    engine._set_selected_pairs([pair])

    assert active.quote_uid not in {
        quote.quote_uid for quote in engine.active_quotes.values()
    }
    assert stale_pair.condition_id not in engine.pairs
    assert engine.metrics.quote_cancellations == 1
    with engine.storage.connect() as db:
        row = db.execute(
            "SELECT status, reason, cancel_ack_at FROM reward_quotes WHERE quote_uid=?",
            (active.quote_uid,),
        ).fetchone()
    assert row["status"] == "cancel_requested"
    assert row["reason"] == "market_unwatched"
    engine._settle_pending_cancels(float(row["cancel_ack_at"]))
    with engine.storage.connect() as db:
        row = db.execute(
            "SELECT status, reason FROM reward_quotes WHERE quote_uid=?",
            (active.quote_uid,),
        ).fetchone()
    assert row["status"] == "cancelled"
    assert row["reason"] == "market_unwatched"


def test_universe_refresh_keeps_risk_only_inventory_market(tmp_path) -> None:
    pair = reward_pair()
    stale_pair = alternate_reward_pair(pair)
    stale_books = reward_books(pair=stale_pair)
    engine = reward_engine(tmp_path, pair=pair)
    stale_plan = yes_buy_plan(stale_pair, stale_books)
    engine._add_inventory(stale_plan, shares=100, price=0.54, now=10.0, quote_uid=None)
    stale_entry = open_reward_quote(engine, stale_pair, stale_plan)
    engine.pairs[stale_pair.condition_id] = stale_pair
    engine.pair_cache[stale_pair.condition_id] = stale_pair

    engine._set_selected_pairs([pair])

    assert stale_pair.condition_id in engine.pairs
    assert stale_entry.quote_uid not in {
        quote.quote_uid for quote in engine.active_quotes.values()
    }
    with engine.storage.connect() as db:
        row = db.execute(
            "SELECT status, reason, cancel_ack_at FROM reward_quotes WHERE quote_uid=?",
            (stale_entry.quote_uid,),
        ).fetchone()
    assert row["status"] == "cancel_requested"
    assert row["reason"] == "market_unwatched"
    engine._settle_pending_cancels(float(row["cancel_ack_at"]))
    with engine.storage.connect() as db:
        row = db.execute(
            "SELECT status, reason FROM reward_quotes WHERE quote_uid=?",
            (stale_entry.quote_uid,),
        ).fetchone()
    assert row["status"] == "cancelled"
    assert row["reason"] == "market_unwatched"


def test_liquidity_reward_uses_local_q_score_scenarios(tmp_path) -> None:
    pair = replace(reward_pair(), minimum_order_age_seconds=0)
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    plans = engine.planner.plan_pair(pair, books, 100)
    assert len(plans) == 2
    for plan in plans:
        active = open_reward_quote(engine, pair, plan, now=10.0)
        active.effective_scoring = True

    engine.market_last_eval_at[pair.condition_id] = 10.0
    engine._account_market_scoring(pair, now=11.0)

    assert engine.metrics.liquidity_q_one_seconds > 0
    assert engine.metrics.liquidity_q_two_seconds > 0
    assert engine.metrics.liquidity_q_min_seconds > 0
    assert engine.metrics.liquidity_reward_conservative_usdc > 0
    assert engine.metrics.liquidity_reward_base_usdc > (
        engine.metrics.liquidity_reward_conservative_usdc
    )
    assert engine.metrics.liquidity_reward_optimistic_usdc > (
        engine.metrics.liquidity_reward_base_usdc
    )
    assert engine.metrics.estimated_reward_usdc == pytest.approx(
        engine.metrics.liquidity_reward_base_usdc
    )


def test_maker_rebate_estimate_is_recorded_on_fill(tmp_path) -> None:
    pair = reward_pair(fee_rate=0.02, fees_enabled=True)
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    plan = yes_buy_plan(pair, books)
    open_reward_quote(engine, pair, plan)

    engine._apply_trade(
        TradePrint(
            token_id=pair.token_a,
            price=Decimal(str(plan.price)),
            size=Decimal("100"),
            side="SELL",
            timestamp_ms=None,
        ),
        now=11.0,
    )

    expected_fee_equivalent = 100 * 0.02 * plan.price * (1.0 - plan.price)
    assert engine.metrics.maker_fee_equivalent_usdc == pytest.approx(
        expected_fee_equivalent
    )
    assert engine.metrics.maker_rebate_base_usdc == pytest.approx(
        expected_fee_equivalent * engine.settings.reward_mm_maker_rebate_rate_base
    )
    with engine.storage.connect() as db:
        fill = db.execute(
            "SELECT * FROM reward_fills WHERE run_id=?", (engine.run_id,)
        ).fetchone()
    assert fill["maker_fee_equivalent_usdc"] == pytest.approx(expected_fee_equivalent)
    assert fill["maker_rebate_base_usdc"] == pytest.approx(
        expected_fee_equivalent * engine.settings.reward_mm_maker_rebate_rate_base
    )


def test_fill_before_cancel_is_counted_during_cancel_latency(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    plan = yes_buy_plan(pair, books)
    active = open_reward_quote(engine, pair, plan, now=10.0)
    engine.active_quotes.pop(plan.key())
    engine._request_cancel_quote(
        active,
        reason="quote_price_changed",
        final_status="replaced",
        now=10.0,
    )

    engine._apply_trade(
        TradePrint(
            token_id=pair.token_a,
            price=Decimal(str(plan.price)),
            size=Decimal("100"),
            side="SELL",
            timestamp_ms=None,
        ),
        now=10.1,
    )

    assert engine.metrics.would_have_fills == 1
    assert engine.metrics.fill_before_cancel == 1
    assert active.quote_uid not in engine.pending_cancel_quotes
    with engine.storage.connect() as db:
        row = db.execute(
            "SELECT lifecycle_state, fill_reason FROM reward_fills WHERE run_id=?",
            (engine.run_id,),
        ).fetchone()
        quote = db.execute(
            "SELECT status FROM reward_quotes WHERE quote_uid=?",
            (active.quote_uid,),
        ).fetchone()
    assert row["lifecycle_state"] == "fill_before_cancel"
    assert row["fill_reason"] == "trade_print_crossed_before_cancel_ack"
    assert quote["status"] == "fill_before_cancel"


def test_cancel_ack_prevents_late_fill(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    plan = yes_buy_plan(pair, books)
    active = open_reward_quote(engine, pair, plan, now=10.0)
    engine.active_quotes.pop(plan.key())
    engine._request_cancel_quote(
        active,
        reason="planner_no_quote",
        final_status="cancelled",
        now=10.0,
    )

    engine._apply_trade(
        TradePrint(
            token_id=pair.token_a,
            price=Decimal(str(plan.price)),
            size=Decimal("100"),
            side="SELL",
            timestamp_ms=None,
        ),
        now=10.4,
    )

    assert engine.metrics.would_have_fills == 0
    assert active.quote_uid not in engine.pending_cancel_quotes
    with engine.storage.connect() as db:
        fills = db.execute(
            "SELECT COUNT(*) AS count FROM reward_fills WHERE run_id=?", (engine.run_id,)
        ).fetchone()
        quote = db.execute(
            "SELECT status FROM reward_quotes WHERE quote_uid=?",
            (active.quote_uid,),
        ).fetchone()
    assert fills["count"] == 0
    assert quote["status"] == "cancelled"


def test_live_readiness_blocks_high_fill_before_cancel_rate(tmp_path) -> None:
    engine = reward_engine(tmp_path)
    engine.metrics.reward_net_conservative_usdc = 1.0
    engine.metrics.would_have_fills = 10
    engine.metrics.fill_before_cancel = 1

    engine._refresh_live_readiness()

    assert engine.metrics.live_readiness_passed == 0
    assert engine.metrics.live_readiness_blockers == 1
    assert engine.metrics.live_readiness_fill_before_cancel_rate == pytest.approx(0.1)


def test_reward_storage_snapshot_returns_new_reward_tables(tmp_path) -> None:
    storage = Storage(tmp_path / "reward.sqlite3")
    storage.init()
    run_id = storage.create_reward_run(1, 100, {"strategy": "reward_mm"})
    pair = reward_pair()
    storage.upsert_reward_markets(run_id, [pair])
    storage.set_reward_metric(run_id, "current_scoring_quotes", 1)
    quote = {
        "quote_uid": "q1",
        "run_id": run_id,
        "condition_id": pair.condition_id,
        "token_id": pair.token_a,
        "outcome": pair.outcome_a,
        "leg": "a",
        "side": "BUY",
        "price": 0.54,
        "size": 100,
        "distance_from_midpoint": 0.02,
        "max_spread": 0.03,
        "score_weight": 11.11,
        "post_only_safe": True,
        "size_eligible": True,
        "locally_scoring": True,
        "status": "open",
        "opened_at": 10.0,
        "updated_at": 10.0,
    }
    storage.insert_reward_quote(quote)
    stale_pair = alternate_reward_pair(pair)
    storage.insert_reward_quote(
        {
            **quote,
            "quote_uid": "stale-q1",
            "condition_id": stale_pair.condition_id,
            "token_id": stale_pair.token_a,
            "outcome": stale_pair.outcome_a,
        }
    )
    storage.insert_reward_quote_snapshots(
        [
            {
                "run_id": run_id,
                "quote_uid": "q1",
                "condition_id": pair.condition_id,
                "token_id": pair.token_a,
                "outcome": pair.outcome_a,
                "side": "BUY",
                "price": 0.54,
                "size": 100,
                "midpoint": 0.56,
                "best_bid": 0.55,
                "best_ask": 0.57,
                "spread": 0.02,
                "distance_from_midpoint": 0.02,
                "max_spread": 0.03,
                "age_seconds": 60,
                "post_only_safe": True,
                "size_eligible": True,
                "within_spread": True,
                "locally_scoring": True,
                "score_weight": 11.11,
                "estimated_reward_usdc": 0.0001,
                "local_ts": 70.0,
            }
        ]
    )

    snapshot = storage.reward_dashboard_snapshot(run_id)

    assert snapshot["metrics"]["current_scoring_quotes"] == 1
    assert snapshot["metrics"]["selected_open_quotes"] == 1
    assert snapshot["metrics"]["stale_open_quotes"] == 1
    assert snapshot["markets"][0]["condition_id"] == pair.condition_id
    assert len(snapshot["quotes"]) == 1
    assert snapshot["quotes"][0]["quote_uid"] == "q1"
    assert snapshot["quotes"][0]["midpoint"] == 0.56


def test_reward_snapshot_splits_matched_unhedged_and_state_errors(tmp_path) -> None:
    storage = Storage(tmp_path / "reward.sqlite3")
    storage.init()
    run_id = storage.create_reward_run(
        1,
        100,
        {
            "strategy": "reward_mm",
            "max_loss_usdc": 10.0,
            "max_position_shares": 500.0,
            "max_exposure_usdc": 1000.0,
        },
    )
    pair = reward_pair()
    broken_pair = replace(
        pair,
        condition_id="0xdef",
        question="Broken state market?",
        token_a="broken-yes-token",
        token_b="broken-no-token",
    )
    storage.upsert_reward_markets(run_id, [pair, broken_pair])
    base = {
        "run_id": run_id,
        "state": "open_inventory",
        "reward_credit_usdc": 0.0,
        "opened_at": 10.0,
        "updated_at": 12.0,
    }
    storage.upsert_reward_inventory(
        {
            **base,
            "condition_id": pair.condition_id,
            "token_id": pair.token_a,
            "outcome": pair.outcome_a,
            "inventory_uid": "inv-yes",
            "shares": 100,
            "avg_price": 0.40,
            "mark_price": 0.45,
            "unrealized_pnl": 5.0,
            "liquidation_pnl": 5.0,
        }
    )
    storage.upsert_reward_inventory(
        {
            **base,
            "condition_id": pair.condition_id,
            "token_id": pair.token_b,
            "outcome": pair.outcome_b,
            "inventory_uid": "inv-no",
            "shares": 60,
            "avg_price": 0.55,
            "mark_price": 0.50,
            "unrealized_pnl": -3.0,
            "liquidation_pnl": -3.0,
        }
    )
    storage.upsert_reward_inventory(
        {
            **base,
            "condition_id": broken_pair.condition_id,
            "token_id": broken_pair.token_a,
            "outcome": broken_pair.outcome_a,
            "inventory_uid": "inv-broken",
            "state": "hedge_locked",
            "shares": 25,
            "avg_price": 0.80,
            "mark_price": 0.20,
            "unrealized_pnl": -15.0,
            "liquidation_pnl": -15.0,
            "closed_at": 11.0,
            "close_mode": "hedge_locked",
        }
    )

    snapshot = storage.reward_dashboard_snapshot(run_id)

    assert len(snapshot["inventory"]) == 2
    assert len(snapshot["state_errors"]) == 1
    assert snapshot["metrics"]["inventory_state_errors"] == 1
    assert snapshot["matched_bundles"][0]["matched_shares"] == 60
    assert snapshot["matched_bundles"][0]["bundle_cost"] == pytest.approx(0.95)
    assert snapshot["matched_bundles"][0]["matched_pnl"] == pytest.approx(3.0)
    yes_row = next(row for row in snapshot["inventory"] if row["token_id"] == pair.token_a)
    no_row = next(row for row in snapshot["inventory"] if row["token_id"] == pair.token_b)
    assert yes_row["unhedged_shares"] == 40
    assert yes_row["unhedged_pnl"] == pytest.approx(2.0)
    assert no_row["unhedged_shares"] == 0


def test_market_filters_reject_volatile_thin_and_extreme(tmp_path) -> None:
    engine = reward_engine(tmp_path)
    engine.settings.reward_mm_max_one_day_change = 0.10
    pair = reward_pair()
    books = reward_books(pair=pair)
    book_a = books.books[pair.token_a]
    book_b = books.books[pair.token_b]

    assert engine._market_passes_filters(pair, book_a, book_b) == (True, "")

    volatile = replace(pair, one_day_price_change=0.5)
    assert engine._market_passes_filters(volatile, book_a, book_b) == (False, "volatility")

    extreme = reward_books(
        pair=pair, yes_bid="0.01", yes_ask="0.03", no_bid="0.97", no_ask="0.99"
    )
    assert engine._market_passes_filters(
        pair, extreme.books[pair.token_a], extreme.books[pair.token_b]
    ) == (False, "extreme")

    thin = BookStore({pair.token_a: pair.condition_id, pair.token_b: pair.condition_id})
    thin.books[pair.token_a] = FastBook(
        pair.token_a,
        bids={Decimal("0.55"): Decimal("50")},
        asks={Decimal("0.57"): Decimal("50")},
        tick_size=Decimal("0.01"),
    )
    thin.books[pair.token_b] = FastBook(
        pair.token_b,
        bids={Decimal("0.43"): Decimal("50")},
        asks={Decimal("0.45"): Decimal("50")},
        tick_size=Decimal("0.01"),
    )
    assert engine._market_passes_filters(
        pair, thin.books[pair.token_a], thin.books[pair.token_b]
    ) == (False, "depth")


def test_market_priority_discounts_volatility(tmp_path) -> None:
    engine = reward_engine(tmp_path)
    engine.settings.reward_mm_selection_mode = "projected"
    calm = replace(reward_pair(), one_day_price_change=0.0)
    volatile = replace(reward_pair(), one_day_price_change=0.1)
    assert engine._market_priority(calm, competitor=0.0) > engine._market_priority(
        volatile, competitor=0.0
    )


def test_game_window_blocks_new_entries(tmp_path) -> None:
    now = 1_000_000.0
    pair = replace(
        reward_pair(), game_start_time=datetime.fromtimestamp(now, tz=UTC)
    )
    engine = reward_engine(tmp_path, pair=pair)

    assert engine._can_plan_entry_or_hedge(pair, now) is False
    clear = now + engine.settings.reward_mm_game_pause_post_seconds + 10
    assert engine._can_plan_entry_or_hedge(pair, clear) is True


def test_entry_distance_widens_with_volatility(tmp_path) -> None:
    engine = reward_engine(tmp_path)
    calm = replace(reward_pair(), one_day_price_change=0.0)
    volatile = replace(reward_pair(), one_day_price_change=0.05)
    assert engine._entry_distance_fraction(volatile) > engine._entry_distance_fraction(calm)


def test_scratch_exit_follows_market_below_avg(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(yes_bid="0.50", yes_ask="0.52", no_bid="0.40", no_ask="0.42")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 100.0
    plan = yes_buy_plan(pair, books)
    engine._add_inventory(plan, shares=100, price=0.58, now=10.0, quote_uid=None)
    item = engine.inventory[(pair.condition_id, pair.token_a)]

    exit_plan = engine._passive_exit_plan(pair, item)

    assert exit_plan is not None
    assert exit_plan.side == "SELL"
    assert exit_plan.price < item.avg_price
    assert exit_plan.price == pytest.approx(
        item.avg_price - engine.settings.reward_mm_scratch_loss_per_share
    )


def test_competitor_q_measured_from_book(tmp_path) -> None:
    pair = replace(reward_pair(), minimum_order_age_seconds=0)
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    assert engine._book_competitor_q(pair) > 0


def test_minute_snapshot_coverage_discounts_fresh_quotes(tmp_path) -> None:
    pair = replace(reward_pair(), minimum_order_age_seconds=0)
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.settings.reward_mm_snapshot_interval_seconds = 10.0
    plan = yes_buy_plan(pair, books)
    active = open_reward_quote(engine, pair, plan, now=100.0)

    assert engine._scoring_coverage(active, now=102.0) == pytest.approx(0.2)
    assert engine._scoring_coverage(active, now=200.0) == pytest.approx(1.0)


def test_net_delta_cap_blocks_new_entries(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.settings.reward_mm_max_net_delta_shares = 50.0
    other = alternate_reward_pair(pair)
    other_books = reward_books(pair=other)
    engine.pairs[other.condition_id] = other
    engine.pair_cache[other.condition_id] = other
    engine._add_inventory(
        yes_buy_plan(other, other_books), shares=100, price=0.54, now=10.0, quote_uid=None
    )

    plans = engine.planner.plan_pair(pair, books, 100)
    assert engine._filter_entry_plans_for_inventory(pair, plans) == []


def test_completion_buy_rests_within_arb_ceiling(tmp_path) -> None:
    # Holding a lone YES leg, the race rests both a passive sell of the held leg and a capped
    # completion buy on the opposite leg, priced so the completed set stays within the arb
    # ceiling (avg + buy_price <= reward_mm_arb_close_max_cost).
    pair = reward_pair()
    books = reward_books(yes_bid="0.59", yes_ask="0.61", no_bid="0.42", no_ask="0.44")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 10.0
    engine._add_inventory(
        yes_buy_plan(pair, books), shares=100, price=0.60, now=10.0, quote_uid=None
    )

    exit_plans = engine._manage_inventory(pair, now=11.0)

    item = engine.inventory[(pair.condition_id, pair.token_a)]
    assert item.shares == 100  # not arb-closed: taker set cost 1.04 is above the ceiling
    completion = next(
        p for p in exit_plans if p.token_id == pair.token_b and p.side == "BUY"
    )
    ceiling = engine.settings.reward_mm_arb_close_max_cost
    assert item.avg_price + completion.price <= ceiling + 1e-9
    # the passive sell of the held leg also rests — the other half of the race
    assert any(p.token_id == pair.token_a and p.side == "SELL" for p in exit_plans)


def test_completion_buy_absent_when_opposite_leg_too_dear(tmp_path) -> None:
    # When even a maker completion cannot keep the set within the ceiling inside the reward zone,
    # no completion buy is offered; only the passive sell rests.
    pair = reward_pair()
    books = reward_books(yes_bid="0.59", yes_ask="0.61", no_bid="0.50", no_ask="0.52")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 10.0
    engine._add_inventory(
        yes_buy_plan(pair, books), shares=100, price=0.60, now=10.0, quote_uid=None
    )

    exit_plans = engine._manage_inventory(pair, now=11.0)

    assert not any(p.token_id == pair.token_b and p.side == "BUY" for p in exit_plans)


def test_prune_reward_snapshots_removes_old_rows(tmp_path) -> None:
    storage = Storage(tmp_path / "reward.sqlite3")
    storage.init()
    run_id = storage.create_reward_run(1, 100, {"strategy": "reward_mm"})

    def snapshot(local_ts: float) -> dict:
        return {
            "run_id": run_id,
            "quote_uid": f"q-{local_ts}",
            "condition_id": "0xabc",
            "token_id": "yes-token",
            "outcome": "Yes",
            "side": "BUY",
            "price": 0.54,
            "size": 100,
            "midpoint": 0.56,
            "best_bid": 0.55,
            "best_ask": 0.57,
            "spread": 0.02,
            "distance_from_midpoint": 0.02,
            "max_spread": 0.03,
            "age_seconds": 60,
            "post_only_safe": True,
            "size_eligible": True,
            "within_spread": True,
            "locally_scoring": True,
            "score_weight": 11.11,
            "estimated_reward_usdc": 0.0001,
            "local_ts": local_ts,
        }

    storage.insert_reward_quote_snapshots([snapshot(10.0), snapshot(100.0)])

    pruned = storage.prune_reward_snapshots(run_id, before_ts=50.0)

    assert pruned == 1
    with storage.connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM reward_quote_snapshots WHERE run_id=?", (run_id,)
        ).fetchone()
    assert remaining["c"] == 1


def test_warmup_blocks_entries_until_elapsed(tmp_path) -> None:
    pair = reward_pair()
    engine = reward_engine(tmp_path, pair=pair)
    engine.settings.reward_mm_warmup_seconds = 20.0
    now = 1000.0
    engine.market_first_seen[pair.condition_id] = now

    assert engine._can_plan_entry_or_hedge(pair, now + 5) is False
    clear = now + engine.settings.reward_mm_warmup_seconds + 1
    assert engine._can_plan_entry_or_hedge(pair, clear) is True


def test_unstable_midpoint_sets_cooldown(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.settings.reward_mm_gap_guard_move = 0.0  # isolate EMA-stability path
    engine.settings.reward_mm_runtime_volatility_range = 0.0
    engine.settings.reward_mm_runtime_jump_move = 0.0
    engine.settings.reward_mm_stability_band = 0.015

    engine._update_market_cooldown(pair, now=100.0)  # establishes EMA, no cooldown
    assert engine._market_on_cooldown(pair.condition_id, 100.0) is False

    books.books[pair.token_a] = FastBook(
        pair.token_a,
        bids={Decimal("0.70"): Decimal("1000")},
        asks={Decimal("0.72"): Decimal("1000")},
        tick_size=Decimal("0.01"),
    )
    engine._update_market_cooldown(pair, now=101.0)  # raw mid jumps far from EMA
    assert engine._market_on_cooldown(pair.condition_id, 101.0) is True


def test_post_close_cooldown_blocks_reentry(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(yes_bid="0.53", yes_ask="0.55", no_bid="0.40", no_ask="0.44")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 4.0  # flat dollar stop; mark loss (0.53-0.58)*100 = -5 trips it
    engine.settings.reward_mm_post_close_cooldown_seconds = 30.0
    plan = yes_buy_plan(pair, books)
    engine._add_inventory(plan, shares=100, price=0.58, now=10.0, quote_uid=None)

    engine._manage_inventory(pair, now=11.0)

    assert engine.inventory[(pair.condition_id, pair.token_a)].shares == 0
    assert engine._market_on_cooldown(pair.condition_id, 11.0) is True
    assert engine._can_plan_entry_or_hedge(pair, 11.0) is False


def test_resolution_filter_rejects_near_resolution(tmp_path) -> None:
    engine = reward_engine(tmp_path)
    engine.settings.reward_mm_min_hours_to_resolution = 48.0
    books = reward_books()
    pair = reward_pair()
    book_a = books.books[pair.token_a]
    book_b = books.books[pair.token_b]

    soon = replace(pair, end_date=datetime.fromtimestamp(time.time() + 3600, tz=UTC))
    assert engine._market_passes_filters(soon, book_a, book_b) == (False, "resolution")

    far = replace(pair, end_date=datetime.fromtimestamp(time.time() + 10 * 86400, tz=UTC))
    assert engine._market_passes_filters(far, book_a, book_b)[0] is True


def test_whitelist_property_parses_csv() -> None:
    settings = Settings(_env_file=None, reward_mm_market_whitelist="0xabc, foo ,")
    assert settings.reward_mm_whitelist == {"0xabc", "foo"}


def test_stop_loss_halt_retires_market_when_enabled(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(yes_bid="0.50", yes_ask="0.52", no_bid="0.46", no_ask="0.48")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.settings.reward_mm_halt_market_on_stop_loss = True
    engine.max_loss_usdc = 1.0
    plan = yes_buy_plan(pair, books)
    engine._add_inventory(plan, shares=100, price=0.56, now=10.0, quote_uid=None)

    engine._manage_inventory(pair, now=11.0)

    # The market is retired for the rest of the run...
    assert pair.condition_id in engine.halted_markets
    assert engine.metrics.halted_markets == 1
    # ...and a fresh cycle quotes nothing on it (fully dark).
    engine._handle_pair(pair, size_shares=100.0)
    assert not any(key[0] == pair.condition_id for key in engine.active_quotes)


def test_stop_loss_does_not_halt_market_by_default(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(yes_bid="0.50", yes_ask="0.52", no_bid="0.46", no_ask="0.48")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 1.0
    plan = yes_buy_plan(pair, books)
    engine._add_inventory(plan, shares=100, price=0.56, now=10.0, quote_uid=None)

    engine._manage_inventory(pair, now=11.0)

    assert pair.condition_id not in engine.halted_markets
    assert engine.metrics.halted_markets == 0


def test_live_fill_clamped_to_order_size(tmp_path) -> None:
    # A taker can sweep several makers and the user channel may report the trade's TOTAL
    # size; the recorded position must never exceed our own resting order size. This is the
    # run-19 fix (a 40-share quote hit by a 500-share taker booked a phantom 500-share position).
    pair = reward_pair()
    books = reward_books(no_bid="0.48", no_ask="0.50")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 100.0
    plan = yes_buy_plan(pair, books, size=40.0)
    open_reward_quote(engine, pair, plan)

    matched = engine.apply_live_fill(pair.token_a, "BUY", 0.53, 500.0, now=11.0)

    assert matched is True
    item = engine.inventory[(pair.condition_id, pair.token_a)]
    assert item.shares == 40.0  # clamped to the 40-share order, NOT the 500-share trade


def _wallet_pos(token_id, size, *, avg=0.5, cur=0.5, outcome="Yes", redeemable=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        token_id=token_id, size=size, avg_price=avg, cur_price=cur,
        outcome=outcome, redeemable=redeemable,
    )


def test_reconcile_adopts_under_tracked_position(tmp_path) -> None:
    # A real wallet position the bot under-tracks (e.g. a dropped fill) is adopted so it is
    # managed and the PnL is accurate (the run-19 Shanghai untracked leg).
    pair = reward_pair()
    engine = reward_engine(tmp_path, pair=pair, books=reward_books(pair=pair))
    n = engine.reconcile_wallet_positions(
        [_wallet_pos(pair.token_a, 40.0, avg=0.55, outcome=pair.outcome_a)], now=10.0
    )
    assert n == 1
    item = engine.inventory[(pair.condition_id, pair.token_a)]
    assert item.shares == 40.0
    assert item.avg_price == pytest.approx(0.55)


def test_reconcile_adopts_only_the_delta(tmp_path) -> None:
    pair = reward_pair()
    engine = reward_engine(tmp_path, pair=pair, books=reward_books(pair=pair))
    engine._adopt_inventory(pair, "a", pair.outcome_a, pair.token_a, 20.0, 0.50, now=9.0)
    engine.reconcile_wallet_positions(
        [_wallet_pos(pair.token_a, 40.0, avg=0.55, outcome=pair.outcome_a)], now=10.0
    )
    item = engine.inventory[(pair.condition_id, pair.token_a)]
    assert item.shares == 40.0  # adopted only the missing 20, not another 40


def test_reconcile_skips_adoption_when_cost_basis_unknown(tmp_path) -> None:
    # data-api with avgPrice=0 (no cost basis): adopting at 0 would make any completion look like
    # free arb and the bot would buy a real opposite leg (the run-55 phantom). Must not adopt.
    pair = reward_pair()
    engine = reward_engine(tmp_path, pair=pair, books=reward_books(pair=pair))
    n = engine.reconcile_wallet_positions(
        [_wallet_pos(pair.token_a, 50.0, avg=0.0, outcome=pair.outcome_a)], now=10.0
    )
    assert n == 0
    assert (pair.condition_id, pair.token_a) not in engine.inventory


def test_reconcile_skips_recently_closed_token(tmp_path) -> None:
    # The bot just sold this leg; the data-api still reports it. Re-adopting would re-open real
    # exposure, so adoption is skipped within the recent-close window.
    pair = reward_pair()
    engine = reward_engine(tmp_path, pair=pair, books=reward_books(pair=pair))
    engine._adopt_inventory(pair, "a", pair.outcome_a, pair.token_a, 50.0, 0.55, now=1.0)
    item = engine.inventory[(pair.condition_id, pair.token_a)]
    engine._close_inventory(item, close_mode="auto_taker_exit", realized_pnl=-4.0, now=100.0)

    n = engine.reconcile_wallet_positions(
        [_wallet_pos(pair.token_a, 50.0, avg=0.55, outcome=pair.outcome_a)], now=110.0
    )
    assert n == 0  # 10s < recent-close window → not re-adopted


def test_zero_cost_inventory_never_buys_opposite_leg(tmp_path) -> None:
    # An inventory item with avg 0 must never trigger taker hedge, completion buy, or bundle lock.
    pair = reward_pair()
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 10.0
    engine._adopt_inventory(pair, "a", pair.outcome_a, pair.token_a, 50.0, 0.0, now=10.0)
    item = engine.inventory[(pair.condition_id, pair.token_a)]
    assert item.avg_price == 0.0

    assert engine._best_taker_hedge(pair, item) is None
    assert engine._passive_completion_plan(pair, item) is None

    exit_plans = engine._manage_inventory(pair, now=11.0)
    assert not any(p.token_id == pair.token_b and p.side == "BUY" for p in exit_plans)
    assert engine.inventory[(pair.condition_id, pair.token_a)].close_mode != "hedge_locked"


def test_reconcile_skips_locked_bundle_both_legs(tmp_path) -> None:
    # run-48 fix: after a bundle lock the wallet holds BOTH legs (No filled, Yes bought to hedge).
    # The matched min(yes, no) is a flat bundle held to redemption, so reconcile must adopt neither
    # leg — otherwise it re-adopts and re-hedges (Yes 100 vs No 50) and raises a false hedge_stop.
    pair = reward_pair()
    engine = reward_engine(tmp_path, pair=pair, books=reward_books(pair=pair))
    n = engine.reconcile_wallet_positions(
        [
            _wallet_pos(pair.token_a, 50.0, avg=0.87, outcome=pair.outcome_a),
            _wallet_pos(pair.token_b, 50.0, avg=0.12, outcome=pair.outcome_b),
        ],
        now=10.0,
    )
    assert n == 0
    assert not engine.inventory


def test_reconcile_adopts_only_unmatched_bundle_excess(tmp_path) -> None:
    # Unbalanced holdings: 80 No vs 50 Yes. The 50/50 is a flat bundle; only the 30-No excess is a
    # real directional position worth adopting.
    pair = reward_pair()
    engine = reward_engine(tmp_path, pair=pair, books=reward_books(pair=pair))
    n = engine.reconcile_wallet_positions(
        [
            _wallet_pos(pair.token_b, 80.0, avg=0.12, outcome=pair.outcome_b),
            _wallet_pos(pair.token_a, 50.0, avg=0.87, outcome=pair.outcome_a),
        ],
        now=10.0,
    )
    assert n == 1
    assert engine.inventory[(pair.condition_id, pair.token_b)].shares == pytest.approx(30.0)
    assert (pair.condition_id, pair.token_a) not in engine.inventory


def test_reconcile_never_reduces_on_lagging_data(tmp_path) -> None:
    # The data-api can lag a just-recorded fill; reconciliation must never reduce a
    # correctly-tracked position based on a smaller wallet reading.
    pair = reward_pair()
    engine = reward_engine(tmp_path, pair=pair, books=reward_books(pair=pair))
    engine._adopt_inventory(pair, "a", pair.outcome_a, pair.token_a, 40.0, 0.50, now=9.0)
    n = engine.reconcile_wallet_positions(
        [_wallet_pos(pair.token_a, 10.0, avg=0.55, outcome=pair.outcome_a)], now=10.0
    )
    assert n == 0
    assert engine.inventory[(pair.condition_id, pair.token_a)].shares == 40.0


def test_reconcile_alerts_untracked_and_ignores_dust(tmp_path) -> None:
    pair = reward_pair()
    engine = reward_engine(tmp_path, pair=pair, books=reward_books(pair=pair))
    n = engine.reconcile_wallet_positions(
        [
            _wallet_pos("0xUNTRACKED_LIVE", 40.0, cur=0.5),  # live off-universe → alert
            _wallet_pos("0xRESOLVED_DUST", 20.0, cur=0.0),  # resolved → ignored
        ],
        now=10.0,
    )
    assert n == 0
    assert not engine.inventory  # nothing adopted (no matching tracked pair)
    with engine.storage.connect() as db:
        kinds = [
            r[0]
            for r in db.execute(
                "SELECT event_type FROM reward_events WHERE run_id=?", (engine.run_id,)
            ).fetchall()
        ]
    assert kinds.count("untracked_position") == 1  # only the live one alerted


def test_live_fill_during_cancel_window_is_recorded(tmp_path) -> None:
    # A live order can fill while its cancel is in flight; that fill must be recorded, not
    # dropped (the run-19 Shanghai untracked No position, where the loss originally started).
    pair = reward_pair()
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 100.0
    plan = yes_buy_plan(pair, books, size=40.0)
    active = open_reward_quote(engine, pair, plan, now=10.0)
    engine.active_quotes.pop(plan.key())
    engine._request_cancel_quote(
        active, reason="quote_price_changed", final_status="replaced", now=10.0
    )
    assert active.quote_uid in engine.pending_cancel_quotes  # still inside the cancel window

    matched = engine.apply_live_fill(pair.token_a, "BUY", float(plan.price), 40.0, now=10.1)

    assert matched is True
    assert engine.metrics.fill_before_cancel == 1
    item = engine.inventory[(pair.condition_id, pair.token_a)]
    assert item.shares == 40.0
    assert active.quote_uid not in engine.pending_cancel_quotes  # consumed by the fill


def test_live_fill_records_actual_order_size(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(no_bid="0.48", no_ask="0.50")
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.max_loss_usdc = 100.0
    plan = yes_buy_plan(pair, books, size=40.0)
    open_reward_quote(engine, pair, plan)

    engine.apply_live_fill(pair.token_a, "BUY", 0.53, 40.0, now=11.0)

    item = engine.inventory[(pair.condition_id, pair.token_a)]
    assert item.shares == 40.0


def test_volatility_gate_drops_jumpy_and_wide_keeps_calm_and_missing() -> None:
    import asyncio

    from polybot.rewards.scanner import ScanConfig, ScoredMarket, _apply_volatility_gate

    def sm(cid: str, token: str, score: float) -> ScoredMarket:
        return ScoredMarket(
            condition_id=cid, question="q", is_weather=True, mid=0.5, rate_per_day=100.0,
            min_size=20.0, max_spread=0.045, competition=300.0, top_depth=400.0, hours_left=40.0,
            one_day_change=0.0, volume_24hr=100.0, projected_reward=1.5, share=0.02,
            risk_factor=1.0, risk_tier="weather", category="weather", score=score,
            token_a=token, token_b=token + "b",
        )

    class FakeClob:
        def __init__(self, vol):
            self.vol = vol

        async def price_volatility(self, token_id, interval="6h"):
            return self.vol.get(token_id)

    cfg = ScanConfig(top=5, max_recent_step=0.025, max_recent_range=0.06)
    markets = [sm("calm", "t1", 3.0), sm("jumpy", "t2", 2.0), sm("wide", "t3", 1.0),
               sm("nodata", "t4", 0.5)]
    clob = FakeClob({"t1": (0.02, 0.01), "t2": (0.03, 0.04), "t3": (0.10, 0.01), "t4": None})

    kept = {m.condition_id for m in asyncio.run(_apply_volatility_gate(clob, markets, cfg))}
    assert kept == {"calm", "nodata"}  # jumpy (step 0.04) and wide (range 0.10) dropped


def test_clob_price_volatility_accepts_price_only_history() -> None:
    class FakeResponse:
        def json(self):
            return {"history": [{"p": "0.50"}, {"p": "0.56"}, {"p": "0.52"}]}

        def raise_for_status(self) -> None:
            return None

    class FakeHttpClient:
        async def request(self, *_args, **_kwargs):
            return FakeResponse()

    async def scenario():
        clob = ClobClient(Settings(_env_file=None), client=FakeHttpClient())
        return await clob.price_volatility("token", interval="6h", fidelity=1)

    assert asyncio.run(scenario()) == pytest.approx((0.06, 0.06))


def test_recent_volatility_gate_uses_10m_and_2m_windows() -> None:
    import asyncio

    from polybot.rewards.scanner import ScanConfig, ScoredMarket, _apply_recent_volatility_gate

    def sm(cid: str, token: str, score: float) -> ScoredMarket:
        return ScoredMarket(
            condition_id=cid, question="q", is_weather=False, mid=0.5, rate_per_day=100.0,
            min_size=20.0, max_spread=0.045, competition=300.0, top_depth=400.0,
            hours_left=40.0, one_day_change=0.0, volume_24hr=100_000.0,
            projected_reward=10.0, share=0.02, risk_factor=1.0, risk_tier="event",
            category="sports", score=score, token_a=token, token_b=token + "b",
        )

    class FakeClob:
        async def price_history(self, token_id, interval="6h"):
            if token_id == "wide":
                return [(0, 0.50), (540, 0.50), (600, 0.63)]
            if token_id == "jump":
                return [(0, 0.50), (500, 0.50), (590, 0.50), (600, 0.535)]
            return [(0, 0.50), (500, 0.505), (600, 0.51)]

    cfg = ScanConfig(
        top=5,
        volatility_10m_reject=0.12,
        volatility_2m_reject=0.03,
    )
    markets = [sm("calm", "calm", 3.0), sm("wide", "wide", 2.0), sm("jump", "jump", 1.0)]

    kept = {
        m.condition_id
        for m in asyncio.run(_apply_recent_volatility_gate(FakeClob(), markets, cfg))
    }
    assert kept == {"calm"}


def test_classify_category_buckets_markets() -> None:
    from polybot.rewards.scanner import classify_category

    assert classify_category("Will the highest temperature in Milan be 34C?") == "weather"
    assert classify_category("Lakers vs Celtics winner") == "sports"
    assert classify_category("Will Arsenal win the Premier League?") == "sports"
    assert classify_category("Who wins?", tags=("Sports", "FIFA World Cup")) == "sports"
    assert classify_category("Who wins?", sport="fifwc", series_id="soccer-fifwc") == "sports"
    assert classify_category("Will Trump win the 2028 election?") == "politics"
    assert classify_category("Will OpenAI release GPT-6 in 2026?") == "tech"
    # Crypto / asset up-down now has its own Finance bucket (was lumped into tech).
    assert classify_category("Will Bitcoin hit 100k?") == "finance"
    assert classify_category("ETH up or down on June 24?") == "finance"
    assert classify_category("Who wins?", tags=("pyth-finance",)) == "finance"
    assert classify_category("Will the Fed cut rates in July?") == "other"
    # event_slug is also considered when the question text is ambiguous.
    assert classify_category("Who advances?", event_slug="nba-finals-2026") == "sports"


def test_scanner_keeps_markets_above_current_size_with_warning() -> None:
    from polybot.rewards.scanner import ScanConfig, _score_pair

    pair = reward_pair(min_size=1000)
    books = {
        pair.token_a: Book.from_api(
            {
                "asset_id": pair.token_a,
                "bids": [{"price": "0.45", "size": "1500"}],
                "asks": [{"price": "0.47", "size": "1500"}],
                "tick_size": "0.01",
            }
        ),
        pair.token_b: Book.from_api(
            {
                "asset_id": pair.token_b,
                "bids": [{"price": "0.53", "size": "1500"}],
                "asks": [{"price": "0.55", "size": "1500"}],
                "tick_size": "0.01",
            }
        ),
    }
    cfg = ScanConfig(size_shares=500, min_midpoint=0.03, min_top_depth=0, max_min_size=0)

    scored = _score_pair(pair, books, cfg, category="sports")

    assert scored is not None
    assert scored.required_size == 1000
    assert scored.quote_size == 1000
    assert scored.size_eligible is False
    assert "requires-size" in scored.flags


def test_scanner_prefilter_limits_reward_min_size_to_selected_size() -> None:
    from polybot.rewards.scanner import ScanConfig, _passes_prefilter

    cfg = ScanConfig(size_shares=50, max_min_size=50)

    assert _passes_prefilter(reward_pair(min_size=20), cfg) is True
    assert _passes_prefilter(reward_pair(min_size=100), cfg) is False


def _two_sided_books(pair, *, size: str):
    """Symmetric YES/NO books with a single resting level each side at `size` shares."""
    return {
        pair.token_a: Book.from_api(
            {
                "asset_id": pair.token_a,
                "bids": [{"price": "0.45", "size": size}],
                "asks": [{"price": "0.47", "size": size}],
                "tick_size": "0.01",
            }
        ),
        pair.token_b: Book.from_api(
            {
                "asset_id": pair.token_b,
                "bids": [{"price": "0.53", "size": size}],
                "asks": [{"price": "0.55", "size": size}],
                "tick_size": "0.01",
            }
        ),
    }


def test_scanner_thin_market_is_graded_and_badged_sweepable() -> None:
    """A book one order can clear is demoted (graded sweep factor) and badged, not zeroed."""
    from polybot.rewards.scanner import ScanConfig, _score_pair

    pair = reward_pair(min_size=20)
    books = _two_sided_books(pair, size="40")
    cfg = ScanConfig(size_shares=100, min_midpoint=0.03, min_top_depth=0, max_min_size=100)

    scored = _score_pair(pair, books, cfg, category="weather")

    assert scored is not None
    # 40 protective shares vs a 400-share target (4 * desired 100) → graded down to the floor
    # band, badged, but never zeroed: reward leads, sweep only nudges.
    assert scored.sweep_shares == 40
    assert cfg.sweep_factor_floor <= scored.sweep_factor < 1
    assert "sweepable" in scored.flags
    assert scored.score == pytest.approx(scored.projected_reward * scored.risk_factor)
    assert scored.score > 0


def test_scanner_reward_leads_thin_high_reward_outranks_deep_low_reward() -> None:
    """A thin low-competition (high-share) book outranks a deep high-competition (low-share) one.

    The deep book is safer (sweep factor 1.0, no badge) but earns far less because its heavy
    resting liquidity collapses our reward share. Ranking is reward-led, so the high earner wins;
    sweep only compresses the gap and flags the thin book ``sweepable`` for the user.
    """
    from polybot.rewards.scanner import ScanConfig, _score_pair

    cfg = ScanConfig(size_shares=100, min_midpoint=0.03, min_top_depth=0, max_min_size=100)
    pair = reward_pair(min_size=20)

    thin = _score_pair(pair, _two_sided_books(pair, size="40"), cfg, category="weather")
    deep = _score_pair(pair, _two_sided_books(pair, size="5000"), cfg, category="weather")

    assert thin is not None and deep is not None
    assert deep.sweep_factor == 1.0 and "sweepable" not in deep.flags
    assert thin.sweep_factor >= cfg.sweep_factor_floor and "sweepable" in thin.flags
    assert thin.projected_reward > deep.projected_reward  # thin = bigger share
    assert thin.score > deep.score  # reward leads the ranking


def test_sweep_factor_spans_floor_to_one() -> None:
    from polybot.rewards.scanner import ScanConfig, _sweep_factor

    cfg = ScanConfig(sweep_target_base=120.0, sweep_target_size_mult=4.0, sweep_factor_floor=0.45)
    assert _sweep_factor(0.0, 20.0, cfg) == pytest.approx(0.45)  # empty front book → floor
    assert _sweep_factor(10_000.0, 20.0, cfg) == pytest.approx(1.0)  # deep → saturates
    mid = _sweep_factor(60.0, 20.0, cfg)  # target = max(120, 80) = 120; 60/120 = 0.5
    assert mid == pytest.approx(0.45 + 0.55 * 0.5)


def test_activity_factor_is_a_mild_liveness_floor_not_a_volume_reward() -> None:
    """Volume does not predict reward, so the factor only mildly discounts a near-dead book."""
    from polybot.rewards.scanner import ScanConfig, _activity_factor

    cfg = ScanConfig(activity_floor_volume=2_000.0, activity_min_factor=0.60)
    assert _activity_factor(5_000.0, cfg) == 1.0  # active → full credit
    assert _activity_factor(2_000.0, cfg) == 1.0  # at floor → full credit
    assert _activity_factor(0.0, cfg) == pytest.approx(0.60)  # dead → mild floor, not annihilated
    assert _activity_factor(1_000.0, cfg) == pytest.approx(0.80)  # halfway up the ramp


def test_reward_realism_factor_scales_projection_without_changing_ranking() -> None:
    from polybot.rewards.scanner import ScanConfig, projected_daily_reward

    base = ScanConfig(size_shares=50, reward_realism_factor=1.0)
    disc = ScanConfig(size_shares=50, reward_realism_factor=0.20)
    full = projected_daily_reward(500.0, 100.0, base, size_shares=50)
    realistic = projected_daily_reward(500.0, 100.0, disc, size_shares=50)
    assert realistic == pytest.approx(full * 0.20)


def test_share_model_matches_real_payout_calibration() -> None:
    """The share formula reproduces the user's two real-money data points once the cap is high.

    Weather 35sh on a $100/day pool earned $2.5 (2.5% share) at the observed competition; a 50sh
    book with the run's ~144 competition realised ~8% share. Both must come out of the formula.
    """
    from polybot.rewards.scanner import ScanConfig, projected_daily_reward

    cfg = ScanConfig(share_cap=0.20, competitor_haircut=1.0, competition_floor=150.0)
    weather = projected_daily_reward(100.0, 341.0, cfg, size_shares=35)
    assert weather == pytest.approx(2.5, abs=0.05)  # 2.5% of $100 → real $2.5/day
    fifty = projected_daily_reward(100.0, 150.0, cfg, size_shares=50)
    assert fifty / 100.0 == pytest.approx(0.077, abs=0.005)  # ~7.7% share at the floor


def test_competition_floor_caps_empty_book_share() -> None:
    """An empty book (competition 0) is floored, not treated as a ~100%-share goldmine."""
    from polybot.rewards.scanner import ScanConfig, projected_daily_reward

    floored = ScanConfig(share_cap=0.50, competitor_haircut=1.0, competition_floor=150.0)
    raw = ScanConfig(share_cap=0.50, competitor_haircut=1.0, competition_floor=0.0)
    # q_self at 50sh = 12.5; floored share = 12.5/(12.5+150) = 7.7%, raw would hit the 50% cap.
    assert projected_daily_reward(100.0, 0.0, floored, size_shares=50) / 100.0 == pytest.approx(
        0.077, abs=0.005
    )
    assert projected_daily_reward(100.0, 0.0, raw, size_shares=50) / 100.0 == pytest.approx(0.50)


def test_selection_count_honors_every_pinned_market() -> None:
    # Without a whitelist, keep the lean --markets target.
    assert RewardMMEngine._selection_count(1, set()) == 1
    assert RewardMMEngine._selection_count(3, set()) == 3
    # A pinned whitelist must never be truncated below its size, even if --markets is
    # smaller (terminal launch or a markets/market_ids mismatch from the UI).
    assert RewardMMEngine._selection_count(1, {"0xa", "0xb", "0xc"}) == 3
    # When --markets exceeds the whitelist, the larger target wins.
    assert RewardMMEngine._selection_count(5, {"0xa", "0xb"}) == 5


def test_adjusted_midpoint_ignores_dust() -> None:
    book = FastBook(
        "t",
        bids={Decimal("0.50"): Decimal("1000"), Decimal("0.56"): Decimal("5")},
        asks={Decimal("0.58"): Decimal("1000"), Decimal("0.57"): Decimal("5")},
        tick_size=Decimal("0.01"),
    )
    assert book.midpoint() == Decimal("0.565")
    assert book.adjusted_midpoint(100) == Decimal("0.54")


def test_markets_paying_reward_counts_above_min_payout(tmp_path) -> None:
    pair = replace(reward_pair(), rewards_rate_per_day=1000.0, minimum_order_age_seconds=0)
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    for plan in engine.planner.plan_pair(pair, books, 100):
        active = open_reward_quote(engine, pair, plan, now=10.0)
        active.effective_scoring = True

    engine.market_competitor_score[pair.condition_id] = 0.0
    engine._publish_metrics()
    assert engine.metrics.markets_paying_reward == 1

    engine.market_competitor_score[pair.condition_id] = 1e9
    engine._publish_metrics()
    assert engine.metrics.markets_paying_reward == 0


def test_keyword_filter_rejects_esports(tmp_path) -> None:
    engine = reward_engine(tmp_path)
    engine.settings.reward_mm_exclude_keywords = "counter-strike,lol,dota"
    books = reward_books()
    pair = reward_pair()
    book_a = books.books[pair.token_a]
    book_b = books.books[pair.token_b]

    assert engine._market_passes_filters(pair, book_a, book_b) == (True, "")

    esports = replace(pair, question="Counter-Strike: TheMongolz vs BetBoom Team")
    assert engine._market_passes_filters(esports, book_a, book_b) == (False, "category")


def test_keyword_filter_empty_by_default_keeps_esports(tmp_path) -> None:
    engine = reward_engine(tmp_path)
    books = reward_books()
    pair = reward_pair()
    esports = replace(pair, question="Counter-Strike: TheMongolz vs BetBoom Team")
    assert engine.settings.reward_mm_exclude_keywords == ""
    assert engine._market_passes_filters(
        esports, books.books[pair.token_a], books.books[pair.token_b]
    )[0] is True


def _set_yes_book(books: BookStore, pair: RewardPair, bid: str, ask: str) -> None:
    books.books[pair.token_a] = FastBook(
        pair.token_a,
        bids={Decimal(bid): Decimal("1000")},
        asks={Decimal(ask): Decimal("1000")},
        tick_size=Decimal("0.01"),
    )


def test_one_day_change_does_not_shrink_entry_below_reward_min_size(tmp_path) -> None:
    pair = replace(
        reward_pair(min_size=50.0),
        one_day_price_change=0.50,
        minimum_order_age_seconds=0,
    )
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.settings.reward_mm_size_vol_k = 5.0

    engine._handle_pair(pair, size_shares=50.0, force_snapshot=True)

    assert len(engine.active_quotes) == 2
    assert all(active.plan.size == pytest.approx(50.0) for active in engine.active_quotes.values())
    assert all(active.effective_scoring for active in engine.active_quotes.values())


def test_entry_depth_multiplier_requires_extra_visible_depth(tmp_path) -> None:
    pair = replace(reward_pair(min_size=50.0), minimum_order_age_seconds=0)
    books = reward_books(pair=pair)
    books.books[pair.token_a] = FastBook(
        pair.token_a,
        bids={Decimal("0.55"): Decimal("40")},
        asks={Decimal("0.57"): Decimal("40")},
        tick_size=Decimal("0.01"),
    )
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.settings.reward_mm_entry_depth_multiplier = 2.0
    plan = yes_buy_plan(pair, books, size=50.0)

    assert engine._entry_size_cap(plan, 50.0) == pytest.approx(40.0)
    engine._handle_pair(pair, size_shares=50.0, force_snapshot=True)

    active_tokens = {active.plan.token_id for active in engine.active_quotes.values()}
    assert pair.token_a not in active_tokens
    assert pair.token_b in active_tokens


def test_runtime_volatility_range_pauses_market(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.settings.reward_mm_gap_guard_move = 0.0
    engine.settings.reward_mm_trend_drift = 0.0
    engine.settings.reward_mm_runtime_jump_move = 0.0
    engine.settings.reward_mm_runtime_volatility_window_seconds = 600.0
    engine.settings.reward_mm_runtime_volatility_range = 0.20

    engine._update_market_cooldown(pair, now=100.0)
    _set_yes_book(books, pair, "0.76", "0.78")  # yes mid 0.77, range 0.21
    engine._update_market_cooldown(pair, now=400.0)

    assert engine._market_on_cooldown(pair.condition_id, 400.0) is True


def test_runtime_jump_pauses_market(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.settings.reward_mm_gap_guard_move = 0.0
    engine.settings.reward_mm_trend_drift = 0.0
    engine.settings.reward_mm_runtime_volatility_range = 0.0
    engine.settings.reward_mm_runtime_jump_window_seconds = 60.0
    engine.settings.reward_mm_runtime_jump_move = 0.05

    engine._update_market_cooldown(pair, now=100.0)
    _set_yes_book(books, pair, "0.61", "0.63")  # yes mid 0.62, jump 0.06
    engine._update_market_cooldown(pair, now=150.0)

    assert engine._market_on_cooldown(pair.condition_id, 150.0) is True


def test_recent_history_volatility_pauses_market_before_first_ws_move(tmp_path) -> None:
    pair = reward_pair()
    engine = reward_engine(tmp_path, pair=pair)
    engine.settings.reward_mm_runtime_jump_move = 0.0
    engine.settings.reward_mm_runtime_volatility_window_seconds = 600.0
    engine.settings.reward_mm_runtime_volatility_range = 0.20

    class FakeClob:
        async def price_history(self, token_id: str, interval: str = "6h", fidelity: int = 1):
            assert token_id == pair.token_a
            return [(100.0, 0.50), (650.0, 0.71)]

    asyncio.run(
        engine._refresh_history_volatility_cooldowns(FakeClob(), [pair], now=650.0)
    )

    assert engine._market_on_cooldown(pair.condition_id, 650.0) is True


def test_recent_history_volatility_timeout_does_not_block_cycle(tmp_path) -> None:
    pair = reward_pair()
    engine = reward_engine(tmp_path, pair=pair)
    engine.settings.reward_mm_runtime_history_timeout_seconds = 0.01

    class SlowClob:
        async def price_history(self, token_id: str, interval: str = "6h", fidelity: int = 1):
            await asyncio.sleep(10.0)
            return [(100.0, 0.50), (650.0, 0.71)]

    start = time.monotonic()
    asyncio.run(engine._refresh_history_volatility_cooldowns(SlowClob(), [pair], now=650.0))

    assert time.monotonic() - start < 0.5
    assert engine._market_on_cooldown(pair.condition_id, 650.0) is False


def test_recent_history_volatility_can_be_disabled(tmp_path) -> None:
    pair = reward_pair()
    engine = reward_engine(tmp_path, pair=pair)
    engine.settings.reward_mm_runtime_history_timeout_seconds = 0.0

    class ExplodingClob:
        async def price_history(self, token_id: str, interval: str = "6h", fidelity: int = 1):
            raise AssertionError("history check should be disabled")

    asyncio.run(
        engine._refresh_history_volatility_cooldowns(ExplodingClob(), [pair], now=650.0)
    )

    assert engine._market_on_cooldown(pair.condition_id, 650.0) is False


def test_trend_detector_pauses_on_directional_drift(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.settings.reward_mm_gap_guard_move = 0.0  # isolate trend detector
    engine.settings.reward_mm_runtime_volatility_range = 0.0
    engine.settings.reward_mm_runtime_jump_move = 0.0
    engine.settings.reward_mm_trend_drift = 0.04  # pin threshold; independent of prod default

    engine._update_market_cooldown(pair, now=100.0)  # baseline yes mid 0.56
    _set_yes_book(books, pair, "0.61", "0.63")  # yes mid 0.62, drift ~0.06 >= 0.04
    engine._update_market_cooldown(pair, now=110.0)

    assert engine._market_on_cooldown(pair.condition_id, 110.0) is True


def test_trend_detector_allows_choppy_market(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.settings.reward_mm_gap_guard_move = 0.0
    engine.settings.reward_mm_runtime_volatility_range = 0.0
    engine.settings.reward_mm_runtime_jump_move = 0.0

    engine._update_market_cooldown(pair, now=100.0)  # mid 0.56
    _set_yes_book(books, pair, "0.57", "0.59")  # mid 0.58 (+0.02)
    engine._update_market_cooldown(pair, now=105.0)
    _set_yes_book(books, pair, "0.55", "0.57")  # back to mid 0.56 (net drift ~0)
    engine._update_market_cooldown(pair, now=110.0)

    assert engine._market_on_cooldown(pair.condition_id, 110.0) is False


def test_book_competitor_q_accepts_explicit_books(tmp_path) -> None:
    pair = reward_pair()
    books = reward_books(pair=pair)
    engine = reward_engine(tmp_path, pair=pair, books=books)
    engine.books = None
    assert engine._book_competitor_q(pair, books) > 0


def test_priority_prefers_low_competition(tmp_path) -> None:
    engine = reward_engine(tmp_path)
    engine.settings.reward_mm_selection_mode = "projected"
    pair = reward_pair()
    assert engine._market_priority(pair, competitor=10.0) > engine._market_priority(
        pair, competitor=100_000.0
    )


def test_projected_daily_reward_respects_competition(tmp_path) -> None:
    engine = reward_engine(tmp_path)
    pair = replace(reward_pair(), rewards_rate_per_day=1000.0)
    assert engine._projected_daily_reward(pair, competitor=0.0) >= 1.0
    assert engine._projected_daily_reward(pair, competitor=1e9) < 1.0


def test_projected_daily_reward_uses_the_run_size_not_the_settings_default(tmp_path) -> None:
    """A quote we will never place must not appear in the projection.

    The share is ``q_self / (q_self + competitor)`` and ``q_self`` scales with size, so reading
    the settings default here reported the reward for the env's size rather than the size the
    operator picked on the launch screen.
    """
    engine = reward_engine(tmp_path)
    pair = replace(reward_pair(), rewards_rate_per_day=1000.0)
    competitor = 500.0

    engine.size_shares = 20.0
    small = engine._projected_daily_reward(pair, competitor=competitor)
    engine.size_shares = 250.0
    large = engine._projected_daily_reward(pair, competitor=competitor)

    assert small < large
    # And it is genuinely the run's size doing the work, not the (unchanged) settings default.
    assert engine.settings.reward_mm_size_shares not in (20.0, 250.0)


def test_selection_mode_volume_is_default_and_ranks_by_activity(tmp_path) -> None:
    engine = reward_engine(tmp_path)
    assert engine.settings.reward_mm_selection_mode == "volume"
    low = replace(reward_pair(), volume_24hr=1000.0)
    high = replace(reward_pair(), volume_24hr=9000.0)
    assert engine._market_priority(high, competitor=0.0) > engine._market_priority(
        low, competitor=0.0
    )


def test_selection_mode_rate_ranks_by_pool_ignoring_volume(tmp_path) -> None:
    engine = reward_engine(tmp_path)
    engine.settings.reward_mm_selection_mode = "rate"
    big_pool = replace(reward_pair(), volume_24hr=1000.0, rewards_rate_per_day=2000.0)
    small_pool = replace(reward_pair(), volume_24hr=9000.0, rewards_rate_per_day=10.0)
    assert engine._market_priority(big_pool, competitor=0.0) > engine._market_priority(
        small_pool, competitor=0.0
    )


class _FakeClob:
    def __init__(self, markets, books, info):
        self._markets = markets
        self._books = books
        self._info = info

    async def get_rewards_markets(
        self, limit, order_by="rate_per_day", position="DESC", next_cursor=None
    ):
        return self._markets, None

    async def get_clob_market_info(self, condition_id):
        return self._info

    async def get_order_books(self, tokens, chunk_size=100):
        return {t: self._books[t] for t in tokens if t in self._books}


def _extreme_market_fixture():
    # midpoint ~0.02 → fails the auto "extreme" filter (min_midpoint default 0.10)
    market = Market(
        condition_id="0xpin",
        question="Pinned reliable market",
        tokens=[Token(token_id="pin-yes", outcome="Yes"), Token(token_id="pin-no", outcome="No")],
        rewards=RewardConfig(min_size=20.0, max_spread=0.03, rates=[RewardRate(rate_per_day=10.0)]),
    )
    books = {
        "pin-yes": Book.from_api(
            {"asset_id": "pin-yes", "bids": [{"price": "0.01", "size": "1000"}],
             "asks": [{"price": "0.03", "size": "1000"}], "tick_size": "0.01"}
        ),
        "pin-no": Book.from_api(
            {"asset_id": "pin-no", "bids": [{"price": "0.97", "size": "1000"}],
             "asks": [{"price": "0.99", "size": "1000"}], "tick_size": "0.01"}
        ),
    }
    info = ClobMarketInfo(
        min_order_size=5.0, tick_size=Decimal("0.01"), fee_info=MarketFeeInfo()
    )
    return market, books, info


def test_whitelist_pins_market_and_bypasses_auto_filters(tmp_path) -> None:
    market, books, info = _extreme_market_fixture()
    clob = _FakeClob([market], books, info)

    # Without whitelist: the extreme-midpoint market is rejected by auto filters.
    engine = RewardMMEngine(Settings(_env_file=None), Storage(tmp_path / "a.sqlite3"))
    assert asyncio.run(engine._discover_pairs(clob, target=1)) == []

    # With the market pinned: it is selected despite failing the auto filters.
    pinned = RewardMMEngine(
        Settings(_env_file=None, reward_mm_market_whitelist="0xpin"),
        Storage(tmp_path / "b.sqlite3"),
    )
    selected = asyncio.run(pinned._discover_pairs(clob, target=1))
    assert [p.condition_id for p in selected] == ["0xpin"]


def test_max_min_size_filter_rejects_expensive_markets(tmp_path) -> None:
    engine = reward_engine(tmp_path)
    engine.settings.reward_mm_max_min_size_shares = 50.0
    books = reward_books()
    cheap = reward_pair(min_size=20)
    expensive = reward_pair(min_size=200)
    book_a = books.books[cheap.token_a]
    book_b = books.books[cheap.token_b]

    assert engine._market_passes_filters(cheap, book_a, book_b)[0] is True
    assert engine._market_passes_filters(expensive, book_a, book_b) == (False, "min_size")


# ---- #2: wake live reconcile on an adverse-direction reprice ----


def test_is_adverse_reprice_only_for_buy_moving_down() -> None:
    from types import SimpleNamespace

    def plan(side, price):
        return SimpleNamespace(side=side, price=price)

    # BUY lowered toward a falling market → adverse (rush it).
    assert RewardMMEngine._is_adverse_reprice(plan("BUY", 0.54), plan("BUY", 0.53)) is True
    # BUY raised (further from the touch) → safer, no rush.
    assert RewardMMEngine._is_adverse_reprice(plan("BUY", 0.54), plan("BUY", 0.55)) is False
    # SELL exits we want to fill → never rushed.
    assert RewardMMEngine._is_adverse_reprice(plan("SELL", 0.56), plan("SELL", 0.57)) is False


class _WakeRecorder:
    def __init__(self) -> None:
        self.wakes = 0
        self.urgent_wakes = 0

    def request_reconcile(self, *, urgent: bool = False) -> None:
        self.wakes += 1
        if urgent:
            self.urgent_wakes += 1


def _thicken_yes_top(engine, pair) -> None:
    """Move the depth-placement tick one cent UP, without moving the midpoint.

    Depth-based placement anchors on the buffer of shares ahead, not on a fixed distance from
    the mid — so shifting the whole ladder does not reprice the quote, but fattening the best
    bid does: 0.55 now carries the target buffer on its own, so the planner steps up to it.
    """
    engine.books.books[pair.token_a] = FastBook(
        pair.token_a,
        bids={
            Decimal("0.55"): Decimal("2200"),
            Decimal("0.54"): Decimal("1000"),
            Decimal("0.53"): Decimal("1000"),
            Decimal("0.52"): Decimal("1000"),
        },
        asks={Decimal("0.57"): Decimal("1000")},
        tick_size=Decimal("0.01"),
    )


def _retick_yes(engine, pair, bid: str, ask: str) -> None:
    engine.books.books[pair.token_a] = FastBook(
        pair.token_a,
        bids=_ladder(bid, step=-1),
        asks={Decimal(ask): Decimal("1000")},
        tick_size=Decimal("0.01"),
    )


def test_handle_pair_wakes_live_on_adverse_buy_reprice(tmp_path) -> None:
    pair = reward_pair(max_spread=0.03)  # default books: yes 0.55/0.57 → YES BUY rests at 0.54
    engine = reward_engine(tmp_path, pair=pair)
    live = _WakeRecorder()
    engine.live = live

    engine._handle_pair(pair, size_shares=100, force_snapshot=True)  # opens entry quotes
    live.wakes = 0  # ignore the initial open

    # YES book ticks down ~1 tick (mid 0.56 → 0.55, well under gap-guard): YES BUY target drops
    # 0.54 → 0.53, an adverse reprice → live is woken immediately.
    _retick_yes(engine, pair, bid="0.54", ask="0.56")
    engine._handle_pair(pair, size_shares=100, force_snapshot=True)
    assert live.wakes >= 1


def test_handle_pair_does_not_wake_live_on_favorable_buy_reprice(tmp_path) -> None:
    pair = reward_pair(max_spread=0.03)
    engine = reward_engine(tmp_path, pair=pair)
    live = _WakeRecorder()
    engine.live = live

    engine._handle_pair(pair, size_shares=100, force_snapshot=True)
    live.wakes = 0
    old = engine.active_quotes[(pair.condition_id, pair.token_a, "BUY")]
    assert old.plan.price == pytest.approx(0.54)

    # YES book ticks up (mid 0.56 -> 0.57). The normal planner would lift the BUY 0.54 -> 0.55
    # for a better reward score, but in live mode the old 0.54 is still scoring, so we keep it
    # rather than moving closer to fill risk immediately.
    _retick_yes(engine, pair, bid="0.56", ask="0.58")
    engine._handle_pair(pair, size_shares=100, force_snapshot=True)
    assert live.wakes == 0
    held = engine.active_quotes[(pair.condition_id, pair.token_a, "BUY")]
    assert held.quote_uid == old.quote_uid
    assert held.plan.price == pytest.approx(0.54)
    assert held.plan.midpoint == pytest.approx(0.57)
    assert held.plan.locally_scoring is True


def test_handle_pair_paper_still_chases_buy_up_for_reward_score(tmp_path) -> None:
    pair = reward_pair(max_spread=0.03)
    engine = reward_engine(tmp_path, pair=pair)

    engine._handle_pair(pair, size_shares=100, force_snapshot=True)
    old_uid = engine.active_quotes[(pair.condition_id, pair.token_a, "BUY")].quote_uid

    _thicken_yes_top(engine, pair)
    engine._handle_pair(pair, size_shares=100, force_snapshot=True)

    active = engine.active_quotes[(pair.condition_id, pair.token_a, "BUY")]
    assert active.quote_uid != old_uid
    assert active.plan.price == pytest.approx(0.55)


def test_handle_pair_live_allows_buy_up_after_quote_age_window(tmp_path) -> None:
    pair = reward_pair(max_spread=0.03)
    engine = reward_engine(tmp_path, pair=pair)
    engine.live = _WakeRecorder()

    engine._handle_pair(pair, size_shares=100, force_snapshot=True)
    active = engine.active_quotes[(pair.condition_id, pair.token_a, "BUY")]
    active.opened_at = time.time() - (
        max(
            engine.settings.reward_mm_live_buy_chase_delay_seconds,
            float(pair.minimum_order_age_seconds),
        )
        + 1.0
    )

    _thicken_yes_top(engine, pair)
    engine._handle_pair(pair, size_shares=100, force_snapshot=True)

    chased = engine.active_quotes[(pair.condition_id, pair.token_a, "BUY")]
    assert chased.quote_uid != active.quote_uid
    assert chased.plan.price == pytest.approx(0.55)

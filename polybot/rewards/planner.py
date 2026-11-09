from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from polybot.feed.books import BookStore, FastBook
from polybot.rewards.models import RewardPair, RewardQuotePlan

# Depth-based placement buffer schedule, keyed on a market's reward ``min_shares`` (a proxy for
# taker lot scale), NOT our order size. Each row is (min_shares, floor_mult, target_mult,
# ceil_mult); the buffer of shares we keep ahead is ``mult * min_shares``. We hold a resting
# quote while depth-ahead stays in [floor, ceil]; when it leaves, we reprice toward ``target``.
# Calibrated 2026-06-23 from a read-only depth/volatility probe of the live reward pool
# (scripts/probe_depth_tiers.py): deep tiers (>=50) hold huge in-zone depth, the thin 20-tier is
# feasibility-capped, and the 250-tier is left to the scanner's volatility screen. Target buffers:
# 20->700, 50->850, 100->2000, 200->4000, 250->5000, 500->7500, 1000->15000.
# The 50-tier aims just above its floor (target 17x vs floor 15x) so the quote sits nearer mid
# for a higher reward score instead of pinned to the reward-zone edge; ceil trimmed to 27x.
DEPTH_TIER_MULTS: tuple[tuple[float, float, float, float], ...] = (
    (20.0, 30.0, 35.0, 45.0),
    (50.0, 15.0, 17.0, 27.0),
    (100.0, 12.0, 20.0, 30.0),
    (200.0, 15.0, 20.0, 25.0),
    (250.0, 15.0, 20.0, 25.0),
    (500.0, 10.0, 15.0, 20.0),
    (1000.0, 10.0, 15.0, 20.0),
)


def depth_mults_for_min_shares(min_shares: float) -> tuple[float, float, float]:
    """Return ``(floor_mult, target_mult, ceil_mult)`` for a market's reward ``min_shares``.

    Snaps to the nearest configured tier by value, so any min_shares (including values between
    or outside the listed tiers) maps to a sensible row.
    """
    best_row = DEPTH_TIER_MULTS[0]
    best_dist = abs(min_shares - best_row[0])
    for row in DEPTH_TIER_MULTS[1:]:
        dist = abs(min_shares - row[0])
        if dist < best_dist:
            best_dist, best_row = dist, row
    return best_row[1], best_row[2], best_row[3]


class RewardQuotePlanner:
    """Builds local, post-only-safe reward quote candidates.

    The first MVP plans BUY quotes on both outcome tokens. In a binary market,
    BUY YES and BUY NO provide liquidity to opposite economic sides without
    requiring inventory for SELL orders.
    """

    def __init__(
        self,
        quote_distance_fraction: float = 0.5,
        *,
        depth_based: bool = False,
    ) -> None:
        self.quote_distance_fraction = min(max(quote_distance_fraction, 0.0), 1.0)
        self.depth_based = depth_based

    def plan_pair(
        self,
        pair: RewardPair,
        books: BookStore,
        size_shares: float,
        *,
        distance_fraction: float | None = None,
    ) -> list[RewardQuotePlan]:
        if pair.rewards_rate_per_day <= 0:
            return []
        max_spread = pair.rewards_max_spread
        if max_spread is None or max_spread <= 0:
            return []
        plans: list[RewardQuotePlan] = []
        for leg, token_id, outcome in (
            ("a", pair.token_a, pair.outcome_a),
            ("b", pair.token_b, pair.outcome_b),
        ):
            book = books.get(token_id)
            if book is None:
                continue
            plan = self.plan_buy(
                pair=pair,
                book=book,
                leg=leg,
                outcome=outcome,
                size_shares=size_shares,
                distance_fraction=distance_fraction,
            )
            if plan is not None:
                plans.append(plan)
        return plans

    def plan_buy(
        self,
        *,
        pair: RewardPair,
        book: FastBook,
        leg: str,
        outcome: str,
        size_shares: float,
        distance_fraction: float | None = None,
        allow_depth: bool = True,
        max_price: float | None = None,
    ) -> RewardQuotePlan | None:
        bid = book.best_bid()
        ask = book.best_ask()
        midpoint = book.midpoint()
        spread = book.spread()
        max_spread = pair.rewards_max_spread
        if bid is None or ask is None or midpoint is None or spread is None or max_spread is None:
            return None
        tick = float(max(pair.tick_size, book.tick_size, Decimal("0.0001")))
        midpoint_f = float(midpoint)
        best_bid = float(bid)
        best_ask = float(ask)
        spread_f = float(spread)
        if best_bid <= 0 or best_ask >= 1 or best_bid >= best_ask or tick <= 0:
            return None

        lower_bound = midpoint_f - max_spread
        post_only_ceiling = best_ask - tick
        if allow_depth and self.depth_based:
            # Depth-based placement: stand behind a buffer of resting shares (scaled to the
            # market's reward min_shares) so a single taker cannot reach us. Returns None when
            # the book is too thin to give floor-level protection inside the reward zone — we
            # simply do not quote that leg.
            min_shares = float(pair.rewards_min_size)
            floor_mult, target_mult, _ceil_mult = depth_mults_for_min_shares(min_shares)
            tick_dec = max(pair.tick_size, book.tick_size, Decimal("0.0001"))
            depth_price = self._depth_target_price(
                book,
                midpoint=midpoint,
                best_bid=bid,
                best_ask=ask,
                max_spread=max_spread,
                tick=tick_dec,
                floor_buffer=floor_mult * min_shares,
                target_buffer=target_mult * min_shares,
            )
            if depth_price is None:
                return None
            price = depth_price
        else:
            fraction = (
                self.quote_distance_fraction
                if distance_fraction is None
                else min(max(distance_fraction, 0.0), 1.0)
            )
            target_distance = max_spread * fraction
            raw_price = midpoint_f - target_distance
            price = self._round_down(raw_price, tick)
            # Never post in front of the resting bid: cap at best_bid so we join the BACK of the
            # queue at that level instead of improving it (an improved bid is the first thing a
            # seller hits — the main avoidable cause of adverse fills). This only ever lowers the
            # price. When the book is wider than the reward zone, best_bid sits below lower_bound;
            # the clamp below then lifts us to the zone edge — the one case we do improve the bid,
            # because otherwise the quote would not score at all.
            price = self._round_down(min(price, best_bid), tick)
        if max_price is not None:
            # Completion/hedge cap: never pay more than this for the leg (keeps the resulting set
            # cost within the arb ceiling). Applied before the zone clamp so the zone floor can
            # still lift it; if it then exceeds the cap, the leg is too dear to complete cheaply.
            price = min(price, self._round_down(max_price, tick))
        if price < lower_bound - 1e-12:
            price = self._round_up(lower_bound, tick)
        if price > post_only_ceiling + 1e-12:
            price = self._round_down(post_only_ceiling, tick)
        if max_price is not None and price > max_price + 1e-12:
            return None
        if price <= 0 or price >= 1:
            return None

        distance = max(midpoint_f - price, 0.0)
        within_spread = distance <= max_spread + 1e-12
        post_only_safe = price <= post_only_ceiling + 1e-12
        min_size = max(pair.rewards_min_size, pair.min_order_size)
        size_eligible = size_shares + 1e-12 >= min_size
        locally_scoring = within_spread and post_only_safe and size_eligible
        score_weight = (
            self._distance_score(max_spread, distance) * size_shares if locally_scoring else 0.0
        )
        reason = ""
        if not size_eligible:
            reason = "below_min_size"
        elif not within_spread:
            reason = "outside_reward_spread"
        elif not post_only_safe:
            reason = "not_post_only_safe"
        return RewardQuotePlan(
            condition_id=pair.condition_id,
            token_id=book.token_id,
            outcome=outcome,
            leg=leg,
            side="BUY",
            price=price,
            size=size_shares,
            midpoint=midpoint_f,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread_f,
            distance_from_midpoint=distance,
            max_spread=max_spread,
            tick_size=tick,
            post_only_safe=post_only_safe,
            size_eligible=size_eligible,
            within_spread=within_spread,
            locally_scoring=locally_scoring,
            score_weight=score_weight,
            reason=reason,
        )

    def plan_sell(
        self,
        *,
        pair: RewardPair,
        book: FastBook,
        leg: str,
        outcome: str,
        size_shares: float,
        minimum_price: float,
    ) -> RewardQuotePlan | None:
        bid = book.best_bid()
        ask = book.best_ask()
        midpoint = book.midpoint()
        spread = book.spread()
        max_spread = pair.rewards_max_spread
        if bid is None or ask is None or midpoint is None or spread is None or max_spread is None:
            return None
        tick = float(max(pair.tick_size, book.tick_size, Decimal("0.0001")))
        midpoint_f = float(midpoint)
        best_bid = float(bid)
        best_ask = float(ask)
        spread_f = float(spread)
        if best_bid <= 0 or best_ask >= 1 or best_bid >= best_ask or tick <= 0:
            return None

        post_only_floor = best_bid + tick
        upper_bound = midpoint_f + max_spread
        raw_price = max(minimum_price, post_only_floor)
        price = self._round_up(raw_price, tick)
        if price <= 0 or price >= 1:
            return None

        distance = max(price - midpoint_f, 0.0)
        within_spread = distance <= max_spread + 1e-12
        post_only_safe = price >= post_only_floor - 1e-12
        min_size = max(pair.rewards_min_size, pair.min_order_size)
        size_eligible = size_shares + 1e-12 >= min_size
        locally_scoring = within_spread and post_only_safe and size_eligible
        score_weight = (
            self._distance_score(max_spread, distance) * size_shares if locally_scoring else 0.0
        )
        reason = ""
        if not size_eligible:
            reason = "below_min_size"
        elif not within_spread:
            reason = "outside_reward_spread"
        elif not post_only_safe:
            reason = "not_post_only_safe"
        elif price > upper_bound + 1e-12:
            reason = "above_reward_zone"
        return RewardQuotePlan(
            condition_id=pair.condition_id,
            token_id=book.token_id,
            outcome=outcome,
            leg=leg,
            side="SELL",
            price=price,
            size=size_shares,
            midpoint=midpoint_f,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread_f,
            distance_from_midpoint=distance,
            max_spread=max_spread,
            tick_size=tick,
            post_only_safe=post_only_safe,
            size_eligible=size_eligible,
            within_spread=within_spread,
            locally_scoring=locally_scoring,
            score_weight=score_weight,
            reason=reason,
        )

    def _depth_target_price(
        self,
        book: FastBook,
        *,
        midpoint: Decimal,
        best_bid: Decimal,
        best_ask: Decimal,
        max_spread: float,
        tick: Decimal,
        floor_buffer: float,
        target_buffer: float,
    ) -> float | None:
        """Choose the BUY tick whose cumulative depth-ahead best matches the target buffer.

        Walks eligible ticks from just under the midpoint (the back of the best bid / the
        post-only ceiling) down to the reward-zone floor. Among the ticks whose depth-ahead
        clears the ``floor_buffer`` we pick the one nearest the ``target_buffer`` — landing in
        the middle of the [floor, ceil] hold band so the quote has room to ride book moves in
        both directions before it must be repriced. Because depth-ahead only grows as we step
        deeper, the shallowest adequately protected tick wins ties, keeping us as close to mid
        (highest reward score) as is safe. Returns None when no in-zone tick offers floor-level
        protection — i.e. the book is too thin to farm here, so we do not quote.
        """
        if tick <= 0:
            return None
        lower_bound = midpoint - Decimal(str(max_spread))
        post_only_ceiling = best_ask - tick
        upper = min(best_bid, post_only_ceiling)
        # Snap the bounds onto the tick grid: the top rounds down, the zone floor rounds up so
        # we never enumerate a price outside the reward zone or above the post-only ceiling.
        upper = (upper / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        floor_price = (lower_bound / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        if upper < floor_price:
            return None
        best_price: Decimal | None = None
        best_delta: float | None = None
        price = upper
        while price >= floor_price - tick / 2:
            depth = float(book.cumulative_depth_ahead("BUY", price))
            if depth + 1e-9 >= floor_buffer:
                delta = abs(depth - target_buffer)
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best_price = price
            price -= tick
        return float(best_price) if best_price is not None else None

    @staticmethod
    def _distance_score(max_spread: float, distance: float) -> float:
        if max_spread <= 0 or distance > max_spread:
            return 0.0
        return max(0.0, (max_spread - distance) / max_spread) ** 2

    @staticmethod
    def _round_down(price: float, tick: float) -> float:
        return math.floor((price + 1e-12) / tick) * tick

    @staticmethod
    def _round_up(price: float, tick: float) -> float:
        return math.ceil((price - 1e-12) / tick) * tick

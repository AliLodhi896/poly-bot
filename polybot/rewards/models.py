from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from polybot.models import Market


def normalize_reward_spread(value: float | None) -> float | None:
    """Return reward max spread as probability units, e.g. 2.5 cents -> 0.025."""
    if value is None:
        return None
    return value / 100 if value > 1 else value


@dataclass(frozen=True)
class RewardPair:
    condition_id: str
    question: str
    event_id: str | None
    event_slug: str | None
    outcome_a: str
    outcome_b: str
    token_a: str
    token_b: str
    volume_24hr: float
    market_spread: float | None
    rewards_min_size: float
    rewards_max_spread: float | None
    rewards_rate_per_day: float
    tick_size: Decimal = Decimal("0.01")
    min_order_size: float = 1.0
    minimum_order_age_seconds: int = 0
    fee_rate: float = 0.0
    fees_enabled: bool = False
    market_slug: str | None = None
    event_title: str | None = None
    game_start_time: datetime | None = None
    one_day_price_change: float = 0.0
    end_date: datetime | None = None
    neg_risk: bool = False
    category: str | None = None
    tags: tuple[str, ...] = ()
    sport: str | None = None
    series_id: str | None = None
    # Outcome-A price from Gamma metadata (``outcomePrices``). Display-only fallback for rows
    # whose books were not hydrated; every gate and estimate still uses live book data.
    metadata_price_a: float | None = None

    @classmethod
    def from_market(
        cls,
        market: Market,
        *,
        tick_size: Decimal = Decimal("0.01"),
        min_order_size: float = 1.0,
        minimum_order_age_seconds: int = 0,
        fee_rate: float = 0.0,
        fees_enabled: bool = False,
        neg_risk: bool = False,
    ) -> RewardPair | None:
        if len(market.tokens) < 2 or market.rewards is None:
            return None
        return cls(
            condition_id=market.condition_id,
            question=market.question,
            event_id=market.event_id,
            event_slug=market.event_slug,
            market_slug=market.slug,
            event_title=market.event_title,
            outcome_a=market.tokens[0].outcome,
            outcome_b=market.tokens[1].outcome,
            token_a=market.tokens[0].token_id,
            token_b=market.tokens[1].token_id,
            volume_24hr=market.volume_24hr,
            market_spread=market.spread,
            rewards_min_size=market.rewards.min_size,
            rewards_max_spread=normalize_reward_spread(market.rewards.max_spread),
            rewards_rate_per_day=market.rewards.rate_per_day,
            tick_size=tick_size,
            min_order_size=min_order_size,
            minimum_order_age_seconds=minimum_order_age_seconds,
            fee_rate=fee_rate,
            fees_enabled=fees_enabled,
            game_start_time=market.game_start_time,
            one_day_price_change=market.one_day_price_change,
            end_date=market.end_date,
            neg_risk=neg_risk,
            category=market.category,
            tags=tuple(market.tags),
            sport=market.sport,
            series_id=market.series_id,
            metadata_price_a=market.tokens[0].price,
        )


@dataclass(frozen=True)
class RewardQuotePlan:
    condition_id: str
    token_id: str
    outcome: str
    leg: str
    side: str
    price: float
    size: float
    midpoint: float
    best_bid: float
    best_ask: float
    spread: float
    distance_from_midpoint: float
    max_spread: float
    tick_size: float
    post_only_safe: bool
    size_eligible: bool
    within_spread: bool
    locally_scoring: bool
    score_weight: float
    reason: str = ""

    def key(self) -> tuple[str, str, str]:
        return (self.condition_id, self.token_id, self.side)

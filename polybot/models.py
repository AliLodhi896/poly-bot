from __future__ import annotations

import json
import math
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


def as_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return default
    return default


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw /= 1000
        return datetime.fromtimestamp(raw, tz=UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def normalize_probability(value: float | None) -> float | None:
    if value is None:
        return None
    if value > 1:
        return value / 100
    return value


class Token(BaseModel):
    token_id: str
    outcome: str
    price: float | None = None
    winner: bool | None = None

    @classmethod
    def from_api(cls, payload: dict[str, Any], outcome: str | None = None) -> Token:
        return cls(
            token_id=str(payload.get("token_id") or payload.get("tokenId") or payload.get("id")),
            outcome=str(payload.get("outcome") or outcome or ""),
            price=as_float(payload.get("price"), default=math.nan)
            if payload.get("price") is not None
            else None,
            winner=payload.get("winner"),
        )


class RewardRate(BaseModel):
    id: int | None = None
    asset_address: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    rate_per_day: float = 0.0
    total_rewards: float = 0.0

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> RewardRate:
        return cls(
            id=payload.get("id"),
            asset_address=payload.get("asset_address") or payload.get("assetAddress"),
            start_date=parse_datetime(payload.get("start_date") or payload.get("startDate")),
            end_date=parse_datetime(payload.get("end_date") or payload.get("endDate")),
            rate_per_day=as_float(
                payload.get("rate_per_day") or payload.get("rewards_daily_rate"), default=0.0
            ),
            total_rewards=as_float(payload.get("total_rewards"), default=0.0),
        )


class RewardConfig(BaseModel):
    min_size: float = 0.0
    max_spread: float | None = None
    rates: list[RewardRate] = Field(default_factory=list)

    @property
    def rate_per_day(self) -> float:
        now = datetime.now(tz=UTC)
        total = 0.0
        for rate in self.rates:
            starts_ok = rate.start_date is None or rate.start_date <= now
            ends_ok = rate.end_date is None or rate.end_date >= now
            if starts_ok and ends_ok:
                total += rate.rate_per_day
        return total

    @classmethod
    def from_gamma(cls, payload: dict[str, Any] | None) -> RewardConfig | None:
        if not payload:
            return None
        rates = [RewardRate.from_api(item) for item in payload.get("rates", [])]
        return cls(
            min_size=as_float(payload.get("min_size") or payload.get("minSize"), default=0.0),
            max_spread=normalize_probability(
                as_float(payload.get("max_spread") or payload.get("maxSpread"), default=0.0)
            ),
            rates=rates,
        )

    @classmethod
    def from_reward_market(cls, payload: dict[str, Any]) -> RewardConfig | None:
        raw_rates = payload.get("rewards_config") or payload.get("rewardsConfig") or []
        if not raw_rates and not payload.get("rewards_min_size"):
            return None
        return cls(
            min_size=as_float(
                payload.get("rewards_min_size") or payload.get("rewardsMinSize"), default=0.0
            ),
            max_spread=normalize_probability(
                as_float(payload.get("rewards_max_spread") or payload.get("rewardsMaxSpread"))
            ),
            rates=[RewardRate.from_api(item) for item in raw_rates],
        )


class Market(BaseModel):
    id: str | None = None
    condition_id: str
    question: str
    slug: str | None = None
    event_id: str | None = None
    event_title: str | None = None
    event_slug: str | None = None
    sport: str | None = None
    series_id: str | None = None
    category: str | None = None
    active: bool = False
    closed: bool = False
    archived: bool = False
    accepting_orders: bool = False
    enable_order_book: bool = False
    end_date: datetime | None = None
    game_start_time: datetime | None = None
    volume_24hr: float = 0.0
    liquidity: float = 0.0
    spread: float | None = None
    one_day_price_change: float = 0.0
    one_hour_price_change: float = 0.0
    neg_risk: bool = False
    tokens: list[Token] = Field(default_factory=list)
    rewards: RewardConfig | None = None
    tags: list[str] = Field(default_factory=list)
    group_item_title: str | None = None
    group_item_threshold: str | None = None
    line: float | None = None

    @property
    def yes_token(self) -> Token | None:
        return next((t for t in self.tokens if t.outcome.lower() in {"yes", "y"}), None)

    @property
    def no_token(self) -> Token | None:
        return next((t for t in self.tokens if t.outcome.lower() in {"no", "n"}), None)

    @classmethod
    def from_gamma(cls, payload: dict[str, Any]) -> Market:
        token_ids = parse_jsonish(payload.get("clobTokenIds"), [])
        outcomes = parse_jsonish(payload.get("outcomes"), [])
        prices = parse_jsonish(payload.get("outcomePrices"), [])
        if not isinstance(token_ids, list):
            token_ids = []
        if not isinstance(outcomes, list):
            outcomes = []
        if not isinstance(prices, list):
            prices = []
        tokens = []
        for index, token_id in enumerate(token_ids):
            tokens.append(
                Token(
                    token_id=str(token_id),
                    outcome=str(outcomes[index]) if index < len(outcomes) else f"outcome_{index}",
                    price=as_float(prices[index]) if index < len(prices) else None,
                )
            )
        if not tokens and isinstance(payload.get("tokens"), list):
            tokens = [Token.from_api(item) for item in payload["tokens"] if isinstance(item, dict)]
        tags_payload = payload.get("tags") or []
        tags = []
        for tag in tags_payload:
            if isinstance(tag, str):
                tags.append(tag)
            elif isinstance(tag, dict):
                tags.append(str(tag.get("slug") or tag.get("label") or tag.get("id")))
        events = payload.get("events") or []
        event_id = None
        event_title = None
        event_slug = None
        if events and isinstance(events[0], dict):
            event_id = str(events[0].get("id")) if events[0].get("id") is not None else None
            event_title = events[0].get("title")
            event_slug = events[0].get("slug")
        return cls(
            id=str(payload.get("id")) if payload.get("id") is not None else None,
            condition_id=str(payload.get("conditionId") or payload.get("condition_id") or ""),
            question=str(payload.get("question") or ""),
            slug=payload.get("slug") or payload.get("market_slug"),
            event_id=event_id or payload.get("event_id"),
            event_title=event_title or payload.get("event_title"),
            event_slug=event_slug or payload.get("event_slug"),
            sport=payload.get("sport"),
            series_id=str(payload["series_id"]) if payload.get("series_id") is not None else None,
            category=payload.get("category"),
            active=bool(payload.get("active", False)),
            closed=bool(payload.get("closed", False)),
            archived=bool(payload.get("archived", False)),
            accepting_orders=bool(
                payload.get("acceptingOrders", payload.get("accepting_orders", False))
            ),
            enable_order_book=bool(
                payload.get("enableOrderBook", payload.get("enable_order_book", False))
            ),
            end_date=parse_datetime(
                payload.get("endDateIso")
                or payload.get("endDate")
                or payload.get("end_date")
                or payload.get("end_date_iso")
            ),
            game_start_time=parse_datetime(
                payload.get("gameStartTime") or payload.get("game_start_time")
            ),
            volume_24hr=as_float(
                payload.get("volume24hrClob")
                or payload.get("volume24hr")
                or payload.get("volume_24hr"),
                default=0.0,
            ),
            liquidity=as_float(
                payload.get("liquidityClob")
                or payload.get("liquidityNum")
                or payload.get("liquidity"),
                default=0.0,
            ),
            spread=as_float(payload.get("spread"), default=0.0)
            if payload.get("spread") is not None
            else None,
            one_day_price_change=as_float(
                payload.get("oneDayPriceChange") or payload.get("one_day_price_change"), default=0.0
            ),
            one_hour_price_change=as_float(
                payload.get("oneHourPriceChange") or payload.get("one_hour_price_change"),
                default=0.0,
            ),
            neg_risk=bool(
                payload.get("negRisk") or payload.get("neg_risk") or payload.get("enableNegRisk")
            ),
            tokens=tokens,
            rewards=RewardConfig.from_gamma(payload.get("rewards"))
            or RewardConfig.from_reward_market(payload),
            tags=tags,
            group_item_title=payload.get("groupItemTitle") or payload.get("group_item_title"),
            group_item_threshold=payload.get("groupItemThreshold")
            or payload.get("group_item_threshold"),
            line=as_float(payload.get("line")) if payload.get("line") not in (None, "") else None,
        )

    @classmethod
    def from_reward_api(cls, payload: dict[str, Any]) -> Market:
        tokens = [Token.from_api(item) for item in payload.get("tokens", [])]
        return cls(
            id=str(payload.get("market_id")) if payload.get("market_id") is not None else None,
            condition_id=str(payload.get("condition_id") or ""),
            question=str(payload.get("question") or ""),
            slug=payload.get("market_slug"),
            event_slug=payload.get("event_slug"),
            game_start_time=parse_datetime(payload.get("game_start_time")),
            active=True,
            closed=False,
            archived=False,
            accepting_orders=True,
            enable_order_book=True,
            end_date=parse_datetime(payload.get("end_date")),
            volume_24hr=as_float(payload.get("volume_24hr"), default=0.0),
            spread=as_float(payload.get("spread"), default=0.0),
            one_day_price_change=as_float(payload.get("one_day_price_change"), default=0.0),
            tokens=tokens,
            rewards=RewardConfig.from_reward_market(payload),
            group_item_title=payload.get("group_item_title"),
        )


class Level(BaseModel):
    price: Decimal
    size: Decimal

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Level:
        return cls(price=Decimal(str(payload.get("price"))), size=Decimal(str(payload.get("size"))))


class Book(BaseModel):
    market: str | None = None
    asset_id: str
    timestamp: float = Field(default_factory=time.time)
    hash: str | None = None
    bids: list[Level] = Field(default_factory=list)
    asks: list[Level] = Field(default_factory=list)
    min_order_size: Decimal = Decimal("1")
    tick_size: Decimal = Decimal("0.01")
    neg_risk: bool = False
    last_trade_price: Decimal | None = None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Book:
        timestamp = as_float(payload.get("timestamp"), default=time.time())
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return cls(
            market=payload.get("market"),
            asset_id=str(
                payload.get("asset_id") or payload.get("assetId") or payload.get("token_id")
            ),
            timestamp=timestamp,
            hash=payload.get("hash"),
            bids=[Level.from_api(item) for item in payload.get("bids", [])],
            asks=[Level.from_api(item) for item in payload.get("asks", [])],
            min_order_size=Decimal(str(payload.get("min_order_size", "1"))),
            tick_size=Decimal(str(payload.get("tick_size", "0.01"))),
            neg_risk=bool(payload.get("neg_risk", False)),
            last_trade_price=Decimal(str(payload["last_trade_price"]))
            if payload.get("last_trade_price") not in (None, "")
            else None,
        ).sorted()

    def sorted(self) -> Book:
        self.bids.sort(key=lambda level: level.price, reverse=True)
        self.asks.sort(key=lambda level: level.price)
        return self

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def midpoint(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / Decimal("2")

    def age_seconds(self, now: float | None = None) -> float:
        return (now or time.time()) - self.timestamp

    def is_stale(self, max_age_seconds: float) -> bool:
        return self.age_seconds() > max_age_seconds

    def depth_shares(self, side: Literal["bids", "asks"], max_levels: int = 5) -> Decimal:
        levels = self.bids if side == "bids" else self.asks
        return sum((level.size for level in levels[:max_levels]), Decimal("0"))


THRESHOLD_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")

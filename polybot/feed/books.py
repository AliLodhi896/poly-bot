from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from polybot.models import Book

Side = Literal["bids", "asks"]


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _timestamp_ms(value: object | None) -> int | None:
    if value in (None, ""):
        return None
    raw = int(float(str(value)))
    return raw if raw >= 10_000_000_000 else raw * 1000


@dataclass
class TradePrint:
    token_id: str
    price: Decimal
    size: Decimal
    side: str
    timestamp_ms: int | None


@dataclass
class FastBook:
    token_id: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    timestamp_ms: int | None = None
    local_updated_ns: int = field(default_factory=time.monotonic_ns)
    tick_size: Decimal = Decimal("0.01")

    @classmethod
    def from_book(cls, book: Book) -> FastBook:
        return cls(
            token_id=book.asset_id,
            bids={level.price: level.size for level in book.bids if level.size > 0},
            asks={level.price: level.size for level in book.asks if level.size > 0},
            timestamp_ms=int(book.timestamp * 1000),
            tick_size=book.tick_size,
        )

    def apply_snapshot(self, payload: dict[str, Any]) -> None:
        self.bids = {
            _decimal(level["price"]): _decimal(level["size"])
            for level in payload.get("bids", [])
            if _decimal(level.get("size", "0")) > 0
        }
        self.asks = {
            _decimal(level["price"]): _decimal(level["size"])
            for level in payload.get("asks", [])
            if _decimal(level.get("size", "0")) > 0
        }
        self.timestamp_ms = _timestamp_ms(payload.get("timestamp")) or self.timestamp_ms
        self.local_updated_ns = time.monotonic_ns()

    def apply_price_change(self, change: dict[str, Any], timestamp: object | None = None) -> None:
        side = str(change.get("side") or change.get("book_side") or "").upper()
        levels = self.bids if side in {"BUY", "BID", "BIDS"} else self.asks
        price = _decimal(change["price"])
        size = _decimal(change.get("size", "0"))
        if size > 0:
            levels[price] = size
        else:
            levels.pop(price, None)
        self.timestamp_ms = _timestamp_ms(timestamp) or self.timestamp_ms
        self.local_updated_ns = time.monotonic_ns()

    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks else None

    def best_bid_depth(self) -> Decimal:
        best = self.best_bid()
        return self.bids.get(best, Decimal("0")) if best is not None else Decimal("0")

    def best_ask_depth(self) -> Decimal:
        best = self.best_ask()
        return self.asks.get(best, Decimal("0")) if best is not None else Decimal("0")

    def cumulative_depth_ahead(
        self, side: str, price: Decimal | float, *, own_size: Decimal | float = 0
    ) -> Decimal:
        """Resting size a taker must consume before reaching an order at ``price``.

        For a BUY we join the bids: a seller sweeps the highest bids first, so the
        shares "ahead" of our order are every bid priced strictly above ``price``
        plus whatever already rests at our own level (we join the BACK of that
        level's time-priority queue). For a SELL we join the asks and the mirror
        holds: a buyer sweeps the lowest asks first, so "ahead" is every ask priced
        strictly below ``price`` plus the same level. ``own_size`` is netted out of
        the same-level total so a live order already resting at ``price`` is not
        counted as protecting itself.
        """
        price_dec = _decimal(price)
        if side == "BUY":
            levels = self.bids
            ahead = sum(
                (size for level, size in levels.items() if level > price_dec),
                Decimal("0"),
            )
        elif side == "SELL":
            levels = self.asks
            ahead = sum(
                (size for level, size in levels.items() if level < price_dec),
                Decimal("0"),
            )
        else:
            raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")
        same_level = max(
            levels.get(price_dec, Decimal("0")) - _decimal(own_size), Decimal("0")
        )
        return ahead + same_level

    def midpoint(self) -> Decimal | None:
        bid = self.best_bid()
        ask = self.best_ask()
        return (bid + ask) / Decimal("2") if bid is not None and ask is not None else None

    def adjusted_midpoint(self, cutoff: Decimal | float = 0) -> Decimal | None:
        """Midpoint from the best bid/ask among levels at or above ``cutoff`` size.

        Mirrors Polymarket's size-cutoff-adjusted midpoint, which ignores dust
        orders. ``cutoff <= 0`` is equivalent to the raw midpoint.
        """
        cutoff_dec = cutoff if isinstance(cutoff, Decimal) else Decimal(str(cutoff))
        bids = [price for price, size in self.bids.items() if size >= cutoff_dec]
        asks = [price for price, size in self.asks.items() if size >= cutoff_dec]
        if not bids or not asks:
            return None
        return (max(bids) + min(asks)) / Decimal("2")

    def spread(self) -> Decimal | None:
        bid = self.best_bid()
        ask = self.best_ask()
        return ask - bid if bid is not None and ask is not None else None

    def imbalance(self) -> Decimal | None:
        bid_depth = self.best_bid_depth()
        ask_depth = self.best_ask_depth()
        total = bid_depth + ask_depth
        return (bid_depth - ask_depth) / total if total > 0 else None

    def microprice(self) -> Decimal | None:
        bid = self.best_bid()
        ask = self.best_ask()
        bid_depth = self.best_bid_depth()
        ask_depth = self.best_ask_depth()
        total = bid_depth + ask_depth
        if bid is None or ask is None or total <= 0:
            return None
        return (ask * bid_depth + bid * ask_depth) / total

    def vwap_buy(self, shares: Decimal) -> Decimal | None:
        return self._vwap(self.asks, shares, reverse=False)

    def vwap_sell(self, shares: Decimal) -> Decimal | None:
        return self._vwap(self.bids, shares, reverse=True)

    def taker_fee_buy_per_share(self, shares: Decimal, fee_rate: float) -> Decimal | None:
        return self._fee_per_share(self.asks, shares, reverse=False, fee_rate=fee_rate)

    def taker_fee_sell_per_share(self, shares: Decimal, fee_rate: float) -> Decimal | None:
        return self._fee_per_share(self.bids, shares, reverse=True, fee_rate=fee_rate)

    def execution_levels(
        self, side: str, shares: Decimal, *, visible_levels: int = 5
    ) -> tuple[dict[str, float], ...]:
        levels = self.asks if side == "BUY" else self.bids
        reverse = side != "BUY"
        remaining = max(shares, Decimal("0"))
        rows: list[dict[str, float]] = []
        for index, price in enumerate(sorted(levels, reverse=reverse)):
            available = levels[price]
            if available <= 0:
                continue
            consumed = min(available, remaining) if remaining > 0 else Decimal("0")
            if index < visible_levels or consumed > 0:
                rows.append(
                    {
                        "level_index": float(index),
                        "price": float(price),
                        "available_shares": float(available),
                        "consumed_shares": float(consumed),
                    }
                )
            remaining -= consumed
            if remaining <= 0 and index + 1 >= visible_levels:
                break
        return tuple(rows)

    def _vwap(
        self, levels: dict[Decimal, Decimal], shares: Decimal, *, reverse: bool
    ) -> Decimal | None:
        if shares <= 0:
            return None
        remaining = shares
        notional = Decimal("0")
        for price in sorted(levels, reverse=reverse):
            size = levels[price]
            if size <= 0:
                continue
            take = min(size, remaining)
            notional += take * price
            remaining -= take
            if remaining <= 0:
                return notional / shares
        return None

    def _fee_per_share(
        self,
        levels: dict[Decimal, Decimal],
        shares: Decimal,
        *,
        reverse: bool,
        fee_rate: float,
    ) -> Decimal | None:
        if shares <= 0:
            return None
        remaining = shares
        fee = Decimal("0")
        rate = Decimal(str(fee_rate))
        for price in sorted(levels, reverse=reverse):
            size = levels[price]
            if size <= 0:
                continue
            take = min(size, remaining)
            fee += take * rate * price * (Decimal("1") - price)
            remaining -= take
            if remaining <= 0:
                return fee / shares
        return None

    def local_age_ms(self) -> float:
        return (time.monotonic_ns() - self.local_updated_ns) / 1_000_000

    def is_stale(self, stale_ms: float) -> bool:
        return self.local_age_ms() > stale_ms


class BookStore:
    def __init__(self, token_to_condition: dict[str, str]) -> None:
        self.books: dict[str, FastBook] = {}
        self.token_to_condition = token_to_condition

    def load_books(self, books: dict[str, Book]) -> None:
        for token_id, book in books.items():
            self.books[token_id] = FastBook.from_book(book)

    def apply_ws_message(self, payload: dict[str, Any]) -> tuple[set[str], list[TradePrint]]:
        event_type = payload.get("event_type")
        affected: set[str] = set()
        trades: list[TradePrint] = []
        if event_type == "book":
            token_id = str(payload.get("asset_id"))
            self.books.setdefault(token_id, FastBook(token_id)).apply_snapshot(payload)
            affected.add(token_id)
        elif event_type == "price_change":
            for change in payload.get("price_changes", []):
                token_id = str(change.get("asset_id"))
                if token_id != "None":
                    self.books.setdefault(token_id, FastBook(token_id)).apply_price_change(
                        change, payload.get("timestamp")
                    )
                    affected.add(token_id)
        elif event_type == "last_trade_price":
            token_id = str(payload.get("asset_id"))
            if token_id != "None" and payload.get("price") not in (None, ""):
                trades.append(
                    TradePrint(
                        token_id=token_id,
                        price=_decimal(payload["price"]),
                        size=_decimal(payload.get("size", "0")),
                        side=str(payload.get("side") or "").upper(),
                        timestamp_ms=_timestamp_ms(payload.get("timestamp")),
                    )
                )
                affected.add(token_id)
        return affected, trades

    def get(self, token_id: str) -> FastBook | None:
        return self.books.get(token_id)

    def affected_conditions(self, token_ids: set[str]) -> set[str]:
        return {
            self.token_to_condition[token_id]
            for token_id in token_ids
            if token_id in self.token_to_condition
        }

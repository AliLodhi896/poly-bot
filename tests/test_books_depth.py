from __future__ import annotations

from decimal import Decimal

import pytest

from polybot.feed.books import FastBook


def _book(bids: dict[str, str], asks: dict[str, str]) -> FastBook:
    return FastBook(
        token_id="t",
        bids={Decimal(p): Decimal(s) for p, s in bids.items()},
        asks={Decimal(p): Decimal(s) for p, s in asks.items()},
        tick_size=Decimal("0.01"),
    )


def test_buy_depth_ahead_includes_higher_bids_and_own_level() -> None:
    # A seller sweeps the highest bids first, so everything priced above us plus
    # whatever already rests at our own level is "ahead".
    book = _book(
        bids={"0.38": "100", "0.37": "200", "0.36": "300"},
        asks={"0.39": "50"},
    )
    assert book.cumulative_depth_ahead("BUY", Decimal("0.38")) == Decimal("100")
    assert book.cumulative_depth_ahead("BUY", Decimal("0.37")) == Decimal("300")
    assert book.cumulative_depth_ahead("BUY", Decimal("0.36")) == Decimal("600")


def test_buy_depth_below_all_bids_counts_whole_side() -> None:
    book = _book(bids={"0.38": "100", "0.37": "200"}, asks={"0.39": "50"})
    # Placing under the lowest bid: every resting bid is ahead, our level is empty.
    assert book.cumulative_depth_ahead("BUY", Decimal("0.30")) == Decimal("300")


def test_buy_depth_at_empty_level_counts_only_strictly_higher() -> None:
    book = _book(bids={"0.38": "100", "0.36": "300"}, asks={"0.39": "50"})
    # 0.37 has no resting size: only the 0.38 level is strictly above it.
    assert book.cumulative_depth_ahead("BUY", Decimal("0.37")) == Decimal("100")


def test_sell_depth_ahead_mirrors_buy_on_asks() -> None:
    # A buyer sweeps the lowest asks first: everything priced below us plus our
    # own level is ahead.
    book = _book(
        bids={"0.38": "100"},
        asks={"0.39": "100", "0.40": "200", "0.41": "300"},
    )
    assert book.cumulative_depth_ahead("SELL", Decimal("0.39")) == Decimal("100")
    assert book.cumulative_depth_ahead("SELL", Decimal("0.40")) == Decimal("300")
    assert book.cumulative_depth_ahead("SELL", Decimal("0.41")) == Decimal("600")


def test_own_size_is_netted_out_of_same_level() -> None:
    book = _book(bids={"0.38": "100", "0.37": "200"}, asks={"0.39": "50"})
    # Our own 20-share order resting at 0.38 must not count itself as protection.
    assert book.cumulative_depth_ahead(
        "BUY", Decimal("0.38"), own_size=Decimal("20")
    ) == Decimal("80")
    # own_size always nets at the evaluated price (here 0.37 is our level):
    # 0.38 (100) + 0.37 (200 - 20) = 280.
    assert book.cumulative_depth_ahead(
        "BUY", Decimal("0.37"), own_size=Decimal("20")
    ) == Decimal("280")


def test_own_size_never_drives_same_level_negative() -> None:
    book = _book(bids={"0.38": "10"}, asks={"0.39": "50"})
    assert book.cumulative_depth_ahead(
        "BUY", Decimal("0.38"), own_size=Decimal("999")
    ) == Decimal("0")


def test_float_price_is_accepted() -> None:
    book = _book(bids={"0.38": "100", "0.37": "200"}, asks={"0.39": "50"})
    assert book.cumulative_depth_ahead("BUY", 0.37) == Decimal("300")


def test_invalid_side_raises() -> None:
    book = _book(bids={"0.38": "100"}, asks={"0.39": "50"})
    with pytest.raises(ValueError):
        book.cumulative_depth_ahead("BID", Decimal("0.38"))


def test_screenshot_book_matches_design_numbers() -> None:
    # The exact ladder from the discussed screenshot (YES bids). These are the
    # depth-ahead figures the placement design is calibrated against.
    book = _book(
        bids={"0.38": "587.82", "0.37": "935.45", "0.36": "433.00", "0.35": "15.68"},
        asks={"0.39": "12.00", "0.40": "275.55", "0.41": "708.00"},
    )
    assert book.cumulative_depth_ahead("BUY", Decimal("0.38")) == Decimal("587.82")
    assert book.cumulative_depth_ahead("BUY", Decimal("0.37")) == Decimal("1523.27")
    assert book.cumulative_depth_ahead("BUY", Decimal("0.36")) == Decimal("1956.27")
    assert book.cumulative_depth_ahead("BUY", Decimal("0.35")) == Decimal("1971.95")

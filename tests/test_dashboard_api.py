"""The snapshot/control API the web UI talks to."""

from __future__ import annotations

from fastapi.testclient import TestClient

from polybot.config import Settings
from polybot.dashboard.server import create_app
from polybot.feed.books import BookStore
from polybot.storage import Storage


def test_book_store_parses_last_trade_prints() -> None:
    """Trade prints drive the reward engine's flow signal, so the parse must stay exact."""
    store = BookStore({"a": "itf-1"})

    affected, trades = store.apply_ws_message(
        {
            "event_type": "last_trade_price",
            "asset_id": "a",
            "price": "0.51",
            "size": "4",
            "side": "BUY",
            "timestamp": "1700000000",
        }
    )

    assert affected == {"a"}
    assert trades[0].side == "BUY"
    assert float(trades[0].price) == 0.51
    assert float(trades[0].size) == 4.0
    assert trades[0].timestamp_ms == 1700000000000


def test_markets_top_groups_by_category(tmp_path, monkeypatch) -> None:
    import polybot.rewards.selector as selector
    from polybot.rewards.selector import SelectedMarket

    def mk(cid: str, cat: str) -> SelectedMarket:
        return SelectedMarket(
            condition_id=cid, question="q", category=cat, rate_per_day=100.0,
            projected_reward=1.5, rank_reward=1.6, min_size=20.0, required_size=20.0,
            size_eligible=True, mid=0.45, safety=88.0, grade="A", risk_label="calm · deep book",
            token_a="a", token_b="b",
        )

    scored = [mk("0x1", "weather"), mk("0x2", "sports"), mk("0x3", "weather"), mk("0x4", "tech")]

    seen_size: list[float | None] = []

    async def fake_select_by_category(_clob, *, settings, per_category, size_shares=None):
        seen_size.append(size_shares)
        grouped = {cat: [] for cat in ("weather", "sports", "politics", "tech", "other")}
        for market in scored[:per_category]:
            grouped.setdefault(market.category, []).append(market)
        return grouped

    monkeypatch.setattr(selector, "select_by_category", fake_select_by_category)

    storage = Storage(tmp_path / "cat.sqlite3")
    storage.init()
    app = create_app(storage, settings=Settings(_env_file=None), control=object())
    body = TestClient(app).get("/api/markets/top?limit=8&size=750").json()

    assert body["error"] is None
    assert seen_size == [750.0]
    by_cat = body["by_category"]
    assert [m["condition_id"] for m in by_cat["weather"]] == ["0x1", "0x3"]
    assert [m["condition_id"] for m in by_cat["sports"]] == ["0x2"]
    assert [m["condition_id"] for m in by_cat["tech"]] == ["0x4"]
    assert by_cat["politics"] == []
    assert by_cat["weather"][0]["category"] == "weather"
    assert by_cat["weather"][0]["required_size"] == 20.0
    assert by_cat["weather"][0]["grade"] == "A"
    assert "details" in by_cat["weather"][0]


def test_markets_top_handles_cancelled_scan_gracefully(tmp_path, monkeypatch) -> None:
    """A scan cancelled mid-flight (Ctrl+C / client disconnect) returns quietly, not a 500."""
    import asyncio

    import polybot.rewards.selector as selector

    async def cancelled_scan(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(selector, "select_by_category", cancelled_scan)

    storage = Storage(tmp_path / "cancel.sqlite3")
    storage.init()
    app = create_app(storage, settings=Settings(_env_file=None), control=object())

    resp = TestClient(app).get("/api/markets/top?limit=8&size=50")

    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] == "scan cancelled"
    assert body["by_category"]["weather"] == []

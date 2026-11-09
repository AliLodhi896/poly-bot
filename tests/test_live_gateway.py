from __future__ import annotations

import asyncio

import pytest

from polybot.api.geoblock import GeoblockResult
from polybot.config import Settings
from polybot.execution.gateway import (
    LiveOrderGateway,
    _extract_batch_order_ids,
    _extract_order_id,
    _parse_canceled,
    _parse_usdc,
)
from polybot.execution.live import LiveTradingDisabled, MissingCredentials
from polybot.feed.user_ws import parse_maker_fills


class _Side:
    BUY = "BUY"
    SELL = "SELL"


class _OrderType:
    GTC = "GTC"
    FOK = "FOK"


class _AssetType:
    COLLATERAL = "COLLATERAL"


def _order_args(**kwargs):
    return {"order_args": kwargs}


def _options(**kwargs):
    return {"options": kwargs}


def _balance_params(**kwargs):
    return {"balance_params": kwargs}


class FakeV2:
    OrderArgs = staticmethod(_order_args)
    PartialCreateOrderOptions = staticmethod(_options)
    BalanceAllowanceParams = staticmethod(_balance_params)
    Side = _Side
    OrderType = _OrderType
    AssetType = _AssetType


class FakeClient:
    def __init__(self) -> None:
        self.posted: list = []
        self.cancelled: list = []
        self.cancel_all_called = 0

    def create_and_post_order(self, order_args, options, order_type, post_only):
        self.posted.append((order_args, options, order_type, post_only))
        return {"orderID": "order-123", "success": True}

    def cancel_orders(self, order_hashes):
        self.cancelled.append(order_hashes)
        return {"canceled": order_hashes}

    def cancel_all(self):
        self.cancel_all_called += 1
        return {"canceled": "all"}

    def get_balance_allowance(self, *_args, **_kwargs):
        return {"balance": "300000000"}  # 300 USDC/pUSD in 6-decimal base units

    def get_open_orders(self, *_args, **_kwargs):
        return [
            {"id": "open-1", "asset_id": "yes-token"},
            {"orderID": "open-2", "market": "no-token"},
            {"nope": "no-id"},  # missing id -> skipped
        ]


def _started_gateway(**overrides) -> LiveOrderGateway:
    settings = Settings(_env_file=None, **overrides)
    return LiveOrderGateway(settings, client=FakeClient(), v2=FakeV2)


def test_parse_usdc_scales_base_units_and_handles_garbage() -> None:
    assert _parse_usdc({"balance": "300000000"}) == pytest.approx(300.0)
    assert _parse_usdc(12.5) == pytest.approx(12.5)
    assert _parse_usdc({"nope": 1}) == 0.0


def test_extract_order_id_variants() -> None:
    assert _extract_order_id({"orderID": "a"}) == "a"
    assert _extract_order_id({"order_id": "b"}) == "b"
    assert _extract_order_id("c") == "c"


def test_extract_batch_order_ids_accepts_id_key_variants() -> None:
    assert _extract_batch_order_ids(
        [
            {"success": True, "orderId": "a"},
            {"success": True, "id": "b"},
            {"success": False, "orderID": "c"},
        ],
        3,
    ) == ["a", "b", None]


def test_parse_canceled_confirms_only_clear_successes() -> None:
    assert _parse_canceled({"canceled": ["a"]}, ["a", "b"]) == {"a"}
    assert _parse_canceled({"cancelled": "b"}, ["a", "b"]) == {"b"}
    assert _parse_canceled({"not_canceled": {"b": "not found"}}, ["a", "b"]) == {"a"}
    assert _parse_canceled({"unexpected": True}, ["a", "b"]) == set()


def test_place_post_only_before_start_raises() -> None:
    gateway = LiveOrderGateway(Settings(_env_file=None))
    with pytest.raises(MissingCredentials):
        gateway.place_post_only("token", "BUY", 0.3, 20, 0.01)


def test_start_blocks_when_live_disabled() -> None:
    gateway = LiveOrderGateway(Settings(_env_file=None, live_enabled=False))
    with pytest.raises(LiveTradingDisabled):
        asyncio.run(gateway.start())


def test_place_post_only_uses_gtc_post_only_and_returns_id() -> None:
    gateway = _started_gateway()
    order_id = gateway.place_post_only("yes-token", "BUY", 0.31, 20, 0.01)
    assert order_id == "order-123"
    client: FakeClient = gateway._client  # type: ignore[assignment]
    order_args, options, order_type, post_only = client.posted[0]
    assert order_args == {
        "order_args": {"token_id": "yes-token", "price": 0.31, "size": 20.0, "side": "BUY"}
    }
    assert options == {"options": {"tick_size": "0.01", "neg_risk": False}}
    assert order_type == "GTC"
    assert post_only is True


def test_cancel_and_balance_and_cancel_all() -> None:
    gateway = _started_gateway()
    assert gateway.cancel("order-123") is True
    gateway.cancel_all()
    client: FakeClient = gateway._client  # type: ignore[assignment]
    assert client.cancelled == [["order-123"]]
    assert client.cancel_all_called == 1
    assert gateway.usdc_balance() == pytest.approx(300.0)


def test_open_orders_returns_id_token_pairs_and_skips_idless() -> None:
    gateway = _started_gateway()
    assert gateway.open_orders() == [
        ("open-1", "yes-token"),
        ("open-2", "no-token"),
    ]


def test_take_fok_posts_fok_not_post_only() -> None:
    gateway = _started_gateway()
    gateway.take_fok("no-token", "BUY", 0.46, 50, 0.01)
    client: FakeClient = gateway._client  # type: ignore[assignment]
    order_args, options, order_type, post_only = client.posted[0]
    assert order_args == {
        "order_args": {"token_id": "no-token", "price": 0.46, "size": 50.0, "side": "BUY"}
    }
    assert order_type == "FOK"
    assert post_only is False


def test_start_succeeds_with_injected_client_and_gates_pass() -> None:
    async def ok_geoblock() -> GeoblockResult:
        return GeoblockResult(blocked=False, country="US")

    settings = Settings(
        _env_file=None,
        live_enabled=True,
        wallet_private_key="0xkey",
        wallet_funder_address="0xfunder",
    )
    gateway = LiveOrderGateway(
        settings,
        client=FakeClient(),
        v2=FakeV2,
        geoblock_check=ok_geoblock,
    )
    result = asyncio.run(gateway.start())
    assert result.blocked is False


def test_parse_maker_fills_reads_maker_orders_and_ignores_non_trade() -> None:
    fills = parse_maker_fills(
        {
            "event_type": "trade",
            "id": "trade-9",
            "asset_id": "yes-token",
            "side": "BUY",  # taker side
            "price": "0.54",
            "size": "200",  # full trade size
            "status": "MATCHED",
            "taker_order_id": "taker-1",
            "maker_orders": [
                {
                    "order_id": "ord-1",
                    "asset_id": "yes-token",
                    "matched_amount": "20",
                    "price": "0.54",
                    "outcome": "Yes",
                }
            ],
        }
    )
    assert len(fills) == 1
    fill = fills[0]
    assert fill.trade_id == "trade-9"
    assert fill.token_id == "yes-token"
    assert fill.price == pytest.approx(0.54)
    assert fill.size == pytest.approx(20.0)  # per-maker matched amount, not the 200 total
    assert fill.order_id == "ord-1"

    assert parse_maker_fills({"event_type": "order", "asset_id": "x"}) == []

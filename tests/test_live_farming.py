from __future__ import annotations

import asyncio
import contextlib
import signal as signalmod
import threading
import time
from types import SimpleNamespace

from polybot.config import Settings
from polybot.feed.user_ws import parse_maker_fills
from polybot.rewards.live import LiveRewardCoordinator, _fok_filled


def test_serve_control_cancels_bot_before_stopping_server(monkeypatch) -> None:
    """On a stop signal the live bot's orders must be cancelled BEFORE the control
    server unwinds, so a slow/stuck server can never strand open orders."""
    import polybot.cli as cli

    events: list[str] = []

    class FakeServer:
        def __init__(self, _config) -> None:
            self.should_exit = False

        def run(self) -> None:
            while not self.should_exit:
                time.sleep(0.005)
            events.append("server_stopped")

    monkeypatch.setattr(cli.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(cli.uvicorn, "Config", lambda *a, **k: None)

    # Capture the handler _serve_control registers instead of touching real OS signals.
    captured: dict[int, object] = {}
    monkeypatch.setattr(
        signalmod, "signal", lambda sig, handler: captured.__setitem__(sig, handler)
    )

    class FakeManager:
        def status(self) -> dict[str, object]:
            return {"running": True}

        def stop(self) -> dict[str, object]:
            events.append("bot_stopped")
            return {}

    def trigger() -> None:
        for _ in range(300):
            handler = captured.get(signalmod.SIGINT)
            if handler is not None:
                handler(signalmod.SIGINT, None)  # type: ignore[operator]
                return
            time.sleep(0.01)

    threading.Thread(target=trigger, daemon=True).start()
    cli._serve_control(None, FakeManager(), host="127.0.0.1", port=0, log_level="warning")

    assert events == ["bot_stopped", "server_stopped"]


def test_manager_start_quotes_every_selected_market(tmp_path, monkeypatch) -> None:
    """A multi-market UI launch must pass --markets >= number of selected markets
    and the full whitelist, so real money is committed to every pinned market."""
    import polybot.control.lock as lock_mod
    import polybot.control.manager as manager_mod
    from polybot.control.manager import BotProcessManager, LaunchParams

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(lock_mod, "held_by", lambda _path: None)

    captured: dict[str, object] = {}

    class FakePopen:
        def __init__(self, argv, env=None, **_kw) -> None:
            captured["argv"] = argv
            captured["env"] = env
            self.pid = 4321
            self.returncode = None

        def poll(self) -> None:
            return None

    monkeypatch.setattr(manager_mod.subprocess, "Popen", FakePopen)

    settings = Settings(
        _env_file=None,
        live_enabled=True,
        reward_mm_micro_live_enabled=True,
        reward_mm_live_require_dry_run=False,
    )
    manager = BotProcessManager(settings)
    ids = ["0xaaa", "0xbbb", "0xccc"]
    # --markets defaults to 1; selecting 3 markets must override it up to 3.
    manager.start(LaunchParams(markets=1, size=35, market_ids=ids))

    argv = captured["argv"]
    assert argv[argv.index("--markets") + 1] == "3"
    assert captured["env"]["POLYBOT_REWARD_MM_MARKET_WHITELIST"] == "0xaaa,0xbbb,0xccc"
    # halt flag defaults off → env var absent.
    assert "POLYBOT_REWARD_MM_HALT_MARKET_ON_STOP_LOSS" not in captured["env"]


def test_manager_start_passes_halt_on_stop_loss_env(tmp_path, monkeypatch) -> None:
    """The unattended-safety toggle must reach the spawned bot as an env flag."""
    import polybot.control.lock as lock_mod
    import polybot.control.manager as manager_mod
    from polybot.control.manager import BotProcessManager, LaunchParams

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(lock_mod, "held_by", lambda _path: None)

    captured: dict[str, object] = {}

    class FakePopen:
        def __init__(self, argv, env=None, **_kw) -> None:
            captured["env"] = env
            self.pid = 4321
            self.returncode = None

        def poll(self) -> None:
            return None

    monkeypatch.setattr(manager_mod.subprocess, "Popen", FakePopen)
    settings = Settings(
        _env_file=None,
        live_enabled=True,
        reward_mm_micro_live_enabled=True,
        reward_mm_live_require_dry_run=False,
    )
    manager = BotProcessManager(settings)
    manager.start(LaunchParams(markets=1, size=35, market_ids=["0xaaa"], halt_on_stop_loss=True))

    assert captured["env"]["POLYBOT_REWARD_MM_HALT_MARKET_ON_STOP_LOSS"] == "true"


class FakeGateway:
    def __init__(self, balance: float = 1000.0, fok_response=None) -> None:
        self.balance = balance
        self.fok_response = fok_response if fok_response is not None else {"status": "matched"}
        self.placed: list = []
        self.cancelled: list = []
        self.cancel_all_calls = 0
        self.fok_calls: list = []
        self._seq = 0
        self.cancel_fails: set = set()  # order_ids whose cancel raises (simulated CLOB failure)
        self.resting: list = []  # (order_id, token_id) returned by open_orders()
        self.cancel_many_batches: list = []  # each batch-cancel call's id list
        self.place_many_batches: list = []  # each batch-place call's spec list
        self.reject_tokens: set = set()  # tokens whose batch place returns None (rejected)
        self.balance_calls = 0  # how many times usdc_balance() was actually hit
        self.open_orders_calls = 0  # how many times open_orders() was actually hit
        self.cancel_attempts: list = []  # every cancel() call (success or simulated failure)
        self.place_raises = False  # when True, place_many raises (ambiguous batch failure)

    def usdc_balance(self) -> float:
        self.balance_calls += 1
        return self.balance

    def place_post_only(self, token_id, side, price, size, tick, neg_risk=False):
        self._seq += 1
        order_id = f"ord-{self._seq}"
        self.placed.append((token_id, side, price, size, order_id, neg_risk))
        return order_id

    def place_many(self, specs):
        # Mirror place_post_only's recording so existing assertions on `placed` hold, and
        # return an order id per spec (None for simulated rejects), aligned by position.
        if self.place_raises:
            raise RuntimeError("Request exception!")
        self.place_many_batches.append(list(specs))
        ids = []
        for token_id, side, price, size, _tick, neg_risk in specs:
            if token_id in self.reject_tokens:
                ids.append(None)
                continue
            self._seq += 1
            order_id = f"ord-{self._seq}"
            self.placed.append((token_id, side, price, size, order_id, neg_risk))
            ids.append(order_id)
        return ids

    def open_orders(self):
        self.open_orders_calls += 1
        return list(self.resting)

    def cancel(self, order_id):
        self.cancel_attempts.append(order_id)
        if order_id in self.cancel_fails:
            raise RuntimeError("order manager not ready, please retry")
        self.cancelled.append(order_id)

    def cancel_many(self, order_ids):
        # Mirror the real batch cancel: confirm all but the simulated failures.
        self.cancel_many_batches.append(list(order_ids))
        canceled = set()
        for oid in order_ids:
            if oid in self.cancel_fails:
                continue  # still resting (not_canceled)
            self.cancelled.append(oid)
            canceled.add(oid)
        return canceled

    def cancel_all(self):
        self.cancel_all_calls += 1

    def take_fok(self, token_id, side, price, size, tick, neg_risk=False):
        self.fok_calls.append((token_id, side, price, size))
        return self.fok_response


def _plan(token_id, side, price, size, weight=1.0, tick=0.01, condition_id="cond"):
    return SimpleNamespace(
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        tick_size=tick,
        score_weight=weight,
        condition_id=condition_id,
    )


def _active(plan):
    return SimpleNamespace(plan=plan)


# Deliberately not the Settings default (100.0): a fill must re-plan at the RUN's size, so a
# test that accidentally reads the settings default fails loudly instead of matching by luck.
RUN_SIZE_SHARES = 37.0


def _engine(active_quotes=None, pairs=None):
    return SimpleNamespace(
        active_quotes=active_quotes or {},
        pairs=pairs or {},
        pair_cache={},
        selected_pairs={},
        run_id=None,
        live=None,
        size_shares=RUN_SIZE_SHARES,
        apply_live_fill_calls=[],
        apply_live_fill=None,
        last_ws_message_at=time.time(),
    )


def _coordinator(gateway, engine):
    return LiveRewardCoordinator(
        Settings(_env_file=None), storage=None, engine=engine, gateway=gateway
    )


def test_fok_filled_parsing() -> None:
    assert _fok_filled({"status": "matched"}) is True
    assert _fok_filled({"status": "unmatched"}) is False
    assert _fok_filled({"success": True}) is True
    assert _fok_filled("???") is False


def test_reconcile_places_then_cancels_and_replaces() -> None:
    gateway = FakeGateway(balance=1000.0)
    engine = _engine(
        {
            ("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50)),
            ("no", "BUY"): _active(_plan("no", "BUY", 0.55, 50)),
        }
    )
    coord = _coordinator(gateway, engine)

    coord.reconcile(balance=1000.0)
    assert len(gateway.placed) == 2
    assert set(coord.live_orders) == {("yes", "BUY"), ("no", "BUY")}

    # drop "no", move "yes" price -> one cancel of no, one cancel+replace of yes
    engine.active_quotes = {("yes", "BUY"): _active(_plan("yes", "BUY", 0.42, 50))}
    coord.reconcile(balance=1000.0)
    assert len(gateway.cancelled) == 2  # old yes + no
    assert set(coord.live_orders) == {("yes", "BUY")}
    assert coord.live_orders[("yes", "BUY")].price == 0.42


def test_reconcile_batches_all_stale_cancels_into_one_call() -> None:
    # On many markets the stale orders must be cancelled in a SINGLE batch request, not one
    # round-trip each, so the reconcile doesn't freeze the event loop (delaying price feed/hedge).
    gateway = FakeGateway(balance=1000.0)
    engine = _engine(
        {
            ("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50)),
            ("no", "BUY"): _active(_plan("no", "BUY", 0.55, 50)),
        }
    )
    coord = _coordinator(gateway, engine)
    coord.reconcile(balance=1000.0)  # places 2, no cancels yet

    engine.active_quotes = {}  # both quotes vanish → both orders stale
    coord.reconcile(balance=1000.0)

    assert len(gateway.cancel_many_batches) == 1  # ONE batch request...
    assert len(gateway.cancel_many_batches[0]) == 2  # ...covering both stale orders
    assert coord.live_orders == {}


def test_reconcile_batches_all_new_places_into_one_call() -> None:
    gateway = FakeGateway(balance=1000.0)
    engine = _engine(
        {
            ("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50)),
            ("no", "BUY"): _active(_plan("no", "BUY", 0.55, 50)),
        }
    )
    coord = _coordinator(gateway, engine)
    coord.reconcile(balance=1000.0)

    assert len(gateway.place_many_batches) == 1  # both new orders in ONE batch request
    assert len(gateway.place_many_batches[0]) == 2
    assert set(coord.live_orders) == {("yes", "BUY"), ("no", "BUY")}


def test_reconcile_batch_place_skips_rejected_order() -> None:
    # A rejected order (no id) must NOT be tracked, so it isn't treated as resting; if the
    # response was ambiguous and a stray order did land, the orphan sweep should still cover the
    # recently-managed token even after the engine drops the desired quote.
    gateway = FakeGateway(balance=1000.0)
    gateway.reject_tokens = {"no"}
    engine = _engine(
        {
            ("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50)),
            ("no", "BUY"): _active(_plan("no", "BUY", 0.55, 50)),
        }
    )
    coord = _coordinator(gateway, engine)
    coord.reconcile(balance=1000.0)

    assert set(coord.live_orders) == {("yes", "BUY")}  # "no" rejected → untracked
    assert coord._orphan_sweep_wake.is_set() is True

    engine.active_quotes = {}
    gateway.resting = [("orphan-no", "no")]
    coord._reap_orphan_orders()
    assert "orphan-no" in gateway.cancelled


def test_extract_batch_order_ids_parses_probe_shape() -> None:
    from polybot.execution.gateway import _extract_batch_order_ids

    # The exact shape returned by the live CLOB (scripts/batch_probe.py).
    resp = [
        {"errorMsg": "", "orderID": "0xaaa", "status": "live", "success": True},
        {"errorMsg": "not enough balance", "orderID": "", "status": "", "success": False},
    ]
    assert _extract_batch_order_ids(resp, 2) == ["0xaaa", None]
    # defensive: wrong length / shape → all None (caller tracks nothing; reaper cleans up)
    assert _extract_batch_order_ids(resp, 3) == [None, None, None]
    assert _extract_batch_order_ids({"unexpected": True}, 2) == [None, None]


def test_reconcile_passes_neg_risk_from_pair() -> None:
    gateway = FakeGateway()
    engine = _engine(
        {("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50, condition_id="cond"))},
        pairs={"cond": SimpleNamespace(neg_risk=True)},
    )
    coord = _coordinator(gateway, engine)
    coord.reconcile(balance=1000.0)
    assert gateway.placed[0][5] is True  # neg_risk flag forwarded to the order


def test_balance_cap_keeps_highest_score_buys() -> None:
    gateway = FakeGateway()
    engine = _engine(
        {
            ("a", "BUY"): _active(_plan("a", "BUY", 0.50, 100, weight=1.0)),  # $50
            ("b", "BUY"): _active(_plan("b", "BUY", 0.50, 100, weight=9.0)),  # $50, higher score
        }
    )
    coord = _coordinator(gateway, engine)
    coord.reconcile(balance=60.0)  # room for only one $50 BUY
    placed_tokens = {p[0] for p in gateway.placed}
    assert placed_tokens == {"b"}  # highest score kept


def test_execute_bundle_lock_buys_opposite_leg() -> None:
    gateway = FakeGateway(fok_response={"status": "matched"})
    coord = _coordinator(gateway, _engine())
    pair = SimpleNamespace(token_a="yes", token_b="no", tick_size=0.01)
    item = SimpleNamespace(token_id="yes", shares=50.0, outcome="Yes")

    assert coord.execute_bundle_lock(pair, item, hedge_price=0.46) is True
    assert gateway.fok_calls == [("no", "BUY", 0.46, 50.0)]

    gateway.fok_response = {"status": "unmatched"}
    assert coord.execute_bundle_lock(pair, item, hedge_price=0.46) is False


def test_alert_emergency_cancels_market_orders() -> None:
    gateway = FakeGateway()
    pair = SimpleNamespace(token_a="yes", token_b="no", tick_size=0.01)
    engine = _engine(
        {
            ("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50)),
            ("other", "BUY"): _active(_plan("other", "BUY", 0.40, 50)),
        },
        pairs={"cond": pair},
    )
    coord = _coordinator(gateway, engine)
    coord.reconcile(balance=1000.0)
    assert ("yes", "BUY") in coord.live_orders

    item = SimpleNamespace(
        condition_id="cond", token_id="yes", shares=50.0, avg_price=0.5, outcome="Yes"
    )
    coord.alert_emergency(item, "liquidation_stop", -5.0)
    # the market's order (yes) cancelled; unrelated market (other) untouched
    assert ("yes", "BUY") not in coord.live_orders
    assert ("other", "BUY") in coord.live_orders
    assert len(gateway.cancelled) == 1


def test_reconcile_keeps_order_when_cancel_fails_and_does_not_duplicate() -> None:
    # Point 1: a failed cancel must not drop the order from tracking, and must not
    # let a repriced replacement stack a second resting order (balance exhaustion).
    gateway = FakeGateway(balance=1000.0)
    engine = _engine({("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50))})
    coord = _coordinator(gateway, engine)
    coord.reconcile(balance=1000.0)
    placed_id = coord.live_orders[("yes", "BUY")].order_id

    # Reprice yes 0.40 -> 0.42 but the CLOB rejects the cancel of the old order.
    gateway.cancel_fails.add(placed_id)
    engine.active_quotes = {("yes", "BUY"): _active(_plan("yes", "BUY", 0.42, 50))}
    coord.reconcile(balance=1000.0)

    # Old order is still tracked (no duplicate placed), at its original price.
    assert set(coord.live_orders) == {("yes", "BUY")}
    assert coord.live_orders[("yes", "BUY")].order_id == placed_id
    assert coord.live_orders[("yes", "BUY")].price == 0.40
    assert len(gateway.placed) == 1  # no replacement placed alongside the live order

    # Once the CLOB accepts the cancel, the next reconcile cancels and replaces.
    gateway.cancel_fails.clear()
    coord.reconcile(balance=1000.0)
    assert coord.live_orders[("yes", "BUY")].price == 0.42
    assert len(gateway.placed) == 2


def test_reap_orphan_orders_cancels_untracked_managed_token_only() -> None:
    # Point 2: an untracked resting order on a managed token is reaped; orders on
    # unmanaged tokens (not ours) and our own tracked order are left alone.
    gateway = FakeGateway()
    engine = _engine({("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50))})
    coord = _coordinator(gateway, engine)
    coord.reconcile(balance=1000.0)
    tracked_id = coord.live_orders[("yes", "BUY")].order_id

    gateway.resting = [
        (tracked_id, "yes"),  # ours, tracked -> keep
        ("orphan-1", "yes"),  # managed token, untracked -> reap
        ("foreign-1", "zzz"),  # unmanaged token -> leave alone
    ]
    coord._reap_orphan_orders()
    assert gateway.cancelled == ["orphan-1"]


def _maker_trade_event(
    *,
    trade_id: str = "trade-1",
    status: str = "MATCHED",
    taker_side: str = "SELL",
    our_order_id: str = "ord-1",
    our_token: str = "yes",
    our_amount: str = "50",
    our_price: str = "0.40",
    total_size: str = "500",
) -> dict:
    """A user-channel trade where OUR resting BUY is one maker among several."""
    return {
        "event_type": "trade",
        "id": trade_id,
        "status": status,
        "side": taker_side,  # taker perspective — opposite of our maker BUY
        "asset_id": our_token,
        "price": our_price,
        "size": total_size,  # full trade size (taker swept several makers)
        "taker_order_id": "taker-1",
        "maker_orders": [
            {
                "order_id": our_order_id,
                "asset_id": our_token,
                "matched_amount": our_amount,
                "price": our_price,
                "outcome": "Yes",
            },
            {
                "order_id": "someone-else",
                "asset_id": our_token,
                "matched_amount": "450",
                "price": our_price,
                "outcome": "Yes",
            },
        ],
    }


def test_parse_maker_fills_reads_per_maker_amounts() -> None:
    fills = parse_maker_fills(_maker_trade_event())
    assert [(f.order_id, f.token_id, f.price, f.size) for f in fills] == [
        ("ord-1", "yes", 0.40, 50.0),
        ("someone-else", "yes", 0.40, 450.0),
    ]
    # Top-level taker side is carried but never used as the fill side.
    assert {f.taker_side for f in fills} == {"SELL"}
    assert parse_maker_fills({"event_type": "order", "maker_orders": []}) == []


def test_on_user_event_applies_maker_fill_with_our_side_and_matched_amount() -> None:
    gateway = FakeGateway()
    calls: list = []
    engine = _engine({("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50))})
    engine.apply_live_fill = lambda token_id, side, price, size, size_shares=None: calls.append(
        (token_id, side, price, size)
    ) or True
    coord = _coordinator(gateway, engine)
    coord.live_orders[("yes", "BUY")] = SimpleNamespace(
        order_id="ord-1", price=0.4, size=50, tick=0.01
    )

    coord.on_user_event(_maker_trade_event())

    # Our side (BUY, from the resting order) and OUR matched amount (50, not the 500 total).
    assert calls == [("yes", "BUY", 0.40, 50.0)]
    assert ("yes", "BUY") not in coord.live_orders


def test_maker_fill_replans_at_the_run_size_not_the_settings_default() -> None:
    """The size that re-plans the replacement quote must be the operator's choice.

    ``size_shares`` is threaded into ``apply_live_fill`` and ends up sizing the quote that
    replaces the filled one. Reading ``settings.reward_mm_size_shares`` here meant a run
    launched at 20 shares re-quoted at the env default — real money, wrong size.
    """
    gateway = FakeGateway()
    seen: list = []
    engine = _engine({("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50))})
    engine.apply_live_fill = lambda token_id, side, price, size, size_shares=None: seen.append(
        size_shares
    ) or True
    coord = _coordinator(gateway, engine)
    coord.live_orders[("yes", "BUY")] = SimpleNamespace(
        order_id="ord-1", price=0.4, size=50, tick=0.01
    )

    coord.on_user_event(_maker_trade_event())

    assert seen == [RUN_SIZE_SHARES]
    assert Settings(_env_file=None).reward_mm_size_shares != RUN_SIZE_SHARES


def test_on_user_event_dedupes_repeated_status_updates() -> None:
    gateway = FakeGateway()
    calls: list = []
    engine = _engine({("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50))})
    engine.apply_live_fill = lambda token_id, side, price, size, size_shares=None: calls.append(
        (token_id, side, price, size)
    ) or True
    coord = _coordinator(gateway, engine)
    coord.live_orders[("yes", "BUY")] = SimpleNamespace(
        order_id="ord-1", price=0.4, size=50, tick=0.01
    )

    coord.on_user_event(_maker_trade_event(status="MATCHED"))
    coord.on_user_event(_maker_trade_event(status="MINED"))
    coord.on_user_event(_maker_trade_event(status="CONFIRMED"))

    assert calls == [("yes", "BUY", 0.40, 50.0)]  # applied exactly once
    assert gateway.cancel_all_calls == 0  # duplicate statuses are not misread as untracked fills


def test_on_user_event_halts_on_untracked_order_fill() -> None:
    gateway = FakeGateway()
    calls: list = []
    engine = _engine()
    engine.apply_live_fill = lambda token_id, side, price, size, size_shares=None: calls.append(
        (token_id, side, price, size)
    )
    coord = _coordinator(gateway, engine)
    # No order with id "ord-1" is tracked -> no guess of side. In live trading this state is too
    # ambiguous to continue unattended, so the coordinator cancels all and stops.
    coord.on_user_event(_maker_trade_event())
    assert calls == []
    assert gateway.cancel_all_calls == 1
    assert coord._stop.is_set() is True


def test_on_user_event_recovers_untracked_fill_we_placed() -> None:
    # An order we placed but dropped from live_orders (cancel/fill race) fills. It is recovered
    # against the engine's pending-cancel quote — side inferred as the opposite of the taker —
    # without halting. The foreign maker in the same event is skipped, not booked.
    gateway = FakeGateway()
    calls: list = []
    engine = _engine()
    engine.apply_live_fill = (
        lambda token_id, side, price, size, size_shares=None, pending_only=False: calls.append(
            (token_id, side, price, size, pending_only)
        )
        or True
    )
    coord = _coordinator(gateway, engine)
    coord._remember_placed_order("ord-1")  # we placed it earlier; since dropped from live_orders
    coord.on_user_event(_maker_trade_event())  # taker SELL -> our maker side BUY
    assert calls == [("yes", "BUY", 0.40, 50.0, True)]  # pending_only recovery, once
    assert gateway.cancel_all_calls == 0
    assert coord._stop.is_set() is False


def test_on_user_event_halts_when_our_untracked_fill_cannot_be_booked() -> None:
    # Our own order (in the placed ledger) fills but the engine has no pending-cancel quote to
    # book it against → fall back to the safe halt + wallet-adoption path.
    gateway = FakeGateway()
    calls: list = []
    engine = _engine()
    engine.apply_live_fill = (
        lambda token_id, side, price, size, size_shares=None, pending_only=False: calls.append(
            (token_id, side, price, size, pending_only)
        )
        or False
    )
    coord = _coordinator(gateway, engine)
    coord._remember_placed_order("ord-1")
    coord.on_user_event(_maker_trade_event())
    assert calls == [("yes", "BUY", 0.40, 50.0, True)]  # recovery attempted, then halt
    assert gateway.cancel_all_calls == 1
    assert coord._stop.is_set() is True


def test_on_user_event_partial_fill_cancels_remaining_order() -> None:
    gateway = FakeGateway()
    calls: list = []
    engine = _engine({("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50))})
    engine.apply_live_fill = lambda token_id, side, price, size, size_shares=None: calls.append(
        (token_id, side, price, size)
    ) or True
    coord = _coordinator(gateway, engine)
    coord.live_orders[("yes", "BUY")] = SimpleNamespace(
        order_id="ord-1", price=0.4, size=50, tick=0.01, remaining=50
    )

    coord.on_user_event(_maker_trade_event(our_amount="10"))

    assert calls == [("yes", "BUY", 0.40, 10.0)]
    assert gateway.cancel_attempts == ["ord-1"]
    assert ("yes", "BUY") not in coord.live_orders


# ---- Phase 3: event-driven reconcile wake ----


def test_request_reconcile_sets_wake_event() -> None:
    coord = _coordinator(FakeGateway(), _engine())
    assert coord._reconcile_wake.is_set() is False
    coord.request_reconcile()
    assert coord._reconcile_wake.is_set() is True


def test_request_reconcile_urgent_sets_bypass_flag() -> None:
    coord = _coordinator(FakeGateway(), _engine())
    assert coord._urgent_reconcile is False
    coord.request_reconcile()  # routine wake keeps the inter-reconcile floor
    assert coord._urgent_reconcile is False
    coord.request_reconcile(urgent=True)  # safety wake bypasses the floor
    assert coord._urgent_reconcile is True
    assert coord._reconcile_wake.is_set() is True


def _many_buy_quotes(n: int) -> dict:
    # n distinct cheap BUY quotes (notional ~0.01 each) so the balance cap never trims them.
    return {
        (f"tok{i}", "BUY"): _active(_plan(f"tok{i}", "BUY", 0.01, 1)) for i in range(n)
    }


def test_place_batches_are_chunked_to_15() -> None:
    # 20 new orders must be split into batches of <=15 (Polymarket rejects >15), and every
    # order must still be placed and tracked — chunking loses nothing.
    gateway = FakeGateway(balance=1000.0)
    coord = _coordinator(gateway, _engine(_many_buy_quotes(20)))
    coord.reconcile(balance=1000.0)
    assert len(gateway.place_many_batches) == 2
    assert all(len(b) <= 15 for b in gateway.place_many_batches)
    assert sum(len(b) for b in gateway.place_many_batches) == 20
    assert len(gateway.placed) == 20
    assert len(coord.live_orders) == 20


def test_place_batch_of_exactly_15_is_a_single_request() -> None:
    # The boundary: exactly 15 stays one batch (not two) — batching is fully preserved at <=15.
    gateway = FakeGateway(balance=1000.0)
    coord = _coordinator(gateway, _engine(_many_buy_quotes(15)))
    coord.reconcile(balance=1000.0)
    assert len(gateway.place_many_batches) == 1
    assert len(gateway.place_many_batches[0]) == 15
    assert len(coord.live_orders) == 15


def test_cancel_batches_are_chunked_to_15() -> None:
    # 20 resting orders that all become stale must be cancelled in batches of <=15.
    gateway = FakeGateway(balance=1000.0)
    engine = _engine(_many_buy_quotes(20))
    coord = _coordinator(gateway, engine)
    coord.reconcile(balance=1000.0)  # place 20
    assert len(coord.live_orders) == 20
    engine.active_quotes = {}  # nothing desired now → all 20 are stale
    coord.reconcile(balance=1000.0)  # cancel 20
    assert gateway.cancel_many_batches  # the batch-cancel path was used
    assert all(len(b) <= 15 for b in gateway.cancel_many_batches)
    assert sum(len(b) for b in gateway.cancel_many_batches) == 20
    assert len(coord.live_orders) == 0


def test_reconcile_loop_reconciles_on_wake_before_periodic_tick() -> None:
    gateway = FakeGateway(balance=1000.0)
    engine = _engine({("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50))})
    coord = _coordinator(gateway, engine)
    # A periodic tick this far out can't fire inside the test window: only a wake can.
    coord.settings.reward_mm_live_reconcile_seconds = 30.0
    coord.settings.reward_mm_live_min_reconcile_seconds = 0.0

    async def scenario() -> None:
        task = asyncio.create_task(coord._reconcile_loop())
        await asyncio.sleep(0)  # let the loop reach its wait
        coord.request_reconcile()

        async def until_placed() -> None:
            while not gateway.placed:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(until_placed(), timeout=1.0)
        coord._stop.set()
        coord.request_reconcile()  # unblock the wait so the task can observe stop
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(scenario())
    assert set(coord.live_orders) == {("yes", "BUY")}


# ---- Phase 4: feed-silence / disconnect guard ----


class _FakeBooks:
    def __init__(self, books: dict) -> None:
        self._books = books

    def get(self, token: str):
        return self._books.get(token)


def test_desired_quotes_keeps_calm_market_but_drops_token_without_a_book() -> None:
    engine = _engine(
        {
            ("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50)),
            ("gone", "BUY"): _active(_plan("gone", "BUY", 0.30, 50)),
        }
    )
    # Feed is fresh. "yes" has a book (even if its price never changes — a calm market must
    # keep scoring); "gone" has no book at all → only that one is dropped.
    engine.books = _FakeBooks({"yes": object()})
    coord = _coordinator(FakeGateway(), engine)
    assert set(coord.desired_quotes()) == {("yes", "BUY")}


def test_desired_quotes_empty_when_whole_feed_is_silent() -> None:
    engine = _engine({("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50))})
    engine.books = _FakeBooks({"yes": object()})
    coord = _coordinator(FakeGateway(), engine)
    # Fresh feed → quote stands.
    assert set(coord.desired_quotes()) == {("yes", "BUY")}
    # No WS message feed-wide for longer than the threshold → we are blind → drop everything.
    engine.last_ws_message_at = time.time() - (
        coord.settings.reward_mm_live_feed_stale_seconds + 5
    )
    assert coord.desired_quotes() == {}


def test_reconcile_cancels_live_orders_when_feed_goes_blind() -> None:
    gateway = FakeGateway(balance=1000.0)
    engine = _engine({("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50))})
    engine.books = _FakeBooks({"yes": object()})
    coord = _coordinator(gateway, engine)

    coord.reconcile(balance=1000.0)
    assert set(coord.live_orders) == {("yes", "BUY")}

    # Whole market feed stalls (real disconnect) → resting orders are pulled (we're blind).
    engine.last_ws_message_at = time.time() - (
        coord.settings.reward_mm_live_feed_stale_seconds + 5
    )
    coord.reconcile(balance=1000.0)
    assert coord.live_orders == {}


# ---- #3 + #1: hot-path hygiene (orphan sweep out of band, cached balance, on-demand sweep) ----


def test_cached_balance_reads_once_within_ttl() -> None:
    gateway = FakeGateway(balance=500.0)
    coord = _coordinator(gateway, _engine())
    coord.settings.reward_mm_live_balance_cache_seconds = 30.0
    assert coord._cached_balance() == 500.0
    gateway.balance = 999.0  # change underneath; cache must hide it within TTL
    assert coord._cached_balance() == 500.0
    assert gateway.balance_calls == 1  # only the first call hit the gateway


def test_reconcile_does_not_reap_orphans_in_hot_path() -> None:
    gateway = FakeGateway(balance=1000.0)
    engine = _engine({("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50))})
    coord = _coordinator(gateway, engine)
    coord.reconcile(balance=1000.0)
    # The reap (and its blocking open_orders round-trip) moved to its own slow loop.
    assert gateway.open_orders_calls == 0


def test_ambiguous_batch_place_failure_requests_orphan_sweep() -> None:
    gateway = FakeGateway(balance=1000.0)
    gateway.place_raises = True
    engine = _engine({("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50))})
    coord = _coordinator(gateway, engine)
    assert coord._orphan_sweep_wake.is_set() is False
    coord.reconcile(balance=1000.0)
    # Nothing tracked (the place errored) and an immediate sweep was requested.
    assert coord.live_orders == {}
    assert coord._orphan_sweep_wake.is_set() is True


def test_orphan_recancel_cooldown_skips_repeat_attempt() -> None:
    gateway = FakeGateway()
    gateway.cancel_fails = {"orphan-1"}  # cancel never confirms → id keeps reappearing
    engine = _engine({("yes", "BUY"): _active(_plan("yes", "BUY", 0.40, 50))})
    coord = _coordinator(gateway, engine)
    coord.settings.reward_mm_live_orphan_recancel_cooldown_seconds = 60.0
    coord.live_orders[("yes", "BUY")] = SimpleNamespace(
        order_id="ord-1", price=0.4, size=50, tick=0.01
    )
    gateway.resting = [("orphan-1", "yes")]

    coord._reap_orphan_orders()
    coord._reap_orphan_orders()  # within cooldown → must NOT re-attempt the same id

    assert gateway.cancel_attempts == ["orphan-1"]  # exactly one attempt despite two sweeps

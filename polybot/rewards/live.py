from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from polybot.config import Settings
from polybot.execution.gateway import LiveOrderGateway
from polybot.feed.user_ws import parse_maker_fills
from polybot.feed.ws import chunked
from polybot.rewards.engine import RewardMMEngine
from polybot.risk import RiskEngine
from polybot.storage import Storage

# Polymarket CLOB caps a single POST /orders (and DELETE /orders) batch at 15 orders — a larger
# batch is rejected with HTTP 400 "too many orders in batch, max allowed: 15". We split larger
# reconcile batches into consecutive requests of this size (batching is preserved within each
# chunk; only the rare >15-order reconcile becomes a few back-to-back requests).
_MAX_ORDERS_PER_BATCH = 15


def _maker_side_from_taker(taker_side: str) -> str | None:
    """Our resting (maker) side is the OPPOSITE of the taker side that matched us.

    A taker SELL hits a resting bid (we were BUY); a taker BUY lifts a resting ask (we were
    SELL). Returns None when the taker side is missing/unrecognised so the caller can fall back.
    """
    t = str(taker_side or "").upper()
    if t in {"SELL", "ASK"}:
        return "BUY"
    if t in {"BUY", "BID"}:
        return "SELL"
    return None


def _fok_filled(response: Any) -> bool:
    """Best-effort: did a FOK marketable order fully fill (vs. killed)?"""
    if isinstance(response, dict):
        status = str(response.get("status") or "").lower()
        if status in {"matched", "filled", "live", "success"}:
            return True
        if status in {"unmatched", "killed", "cancelled", "canceled"}:
            return False
        if response.get("success") is True and not response.get("error"):
            return True
    return False


@dataclass
class LiveOrder:
    order_id: str
    price: float
    size: float
    tick: float
    remaining: float | None = None


class LiveRewardCoordinator:
    """Drives real CLOB orders from the paper engine's desired quotes (Phase B-1).

    The engine computes the desired quote set and inventory logic exactly as in paper;
    this coordinator (a) reconciles those desired quotes to real post-only orders under a
    balance cap, (b) feeds real user-channel fills back into the engine, (c) locks profitable
    bundles with a real FOK taker, and (d) on a loss-stop cancels and alerts the user rather
    than auto-selling at a loss. All real order calls go through :class:`LiveOrderGateway`.
    """

    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        engine: RewardMMEngine,
        gateway: LiveOrderGateway,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.engine = engine
        self.gateway = gateway
        self.risk = RiskEngine(settings)
        self.live_orders: dict[tuple[str, str], LiveOrder] = {}
        self._stop = asyncio.Event()
        # Set by the engine to wake the reconcile loop immediately on an adverse quote pull or
        # a fill, instead of waiting for the next periodic tick. Coalesced: a burst of sets
        # collapses into one reconcile when the loop wakes.
        self._reconcile_wake = asyncio.Event()
        # Set alongside the wake when the trigger is a safety action (an adverse pull / a fill):
        # the loop then skips its inter-reconcile floor so an endangered order is cancelled with
        # minimal latency instead of waiting up to ``min_reconcile_seconds``. Cleared each tick.
        self._urgent_reconcile = False
        # Dedupe maker fills: the same trade is delivered once per status transition
        # (MATCHED -> MINED -> CONFIRMED), so a fill must be applied to inventory exactly
        # once, keyed by (trade_id, maker order_id). Bounded so a long run can't grow it
        # without limit; evicting an ancient key is harmless (that trade is long settled).
        self._applied_fills: set[tuple[str, str]] = set()
        self._applied_fill_keys: deque[tuple[str, str]] = deque()
        # Ledger of every exchange order id we have placed (retained a while after the order
        # leaves live_orders). A user-channel trade event can list OTHER makers' orders too, so an
        # untracked fill is only treated as ours — and recovered — when its order id is in here.
        self._placed_order_ids: set[str] = set()
        self._placed_order_id_queue: deque[str] = deque()
        # Hot-path hygiene (kept out of the per-reconcile blocking round-trips):
        # cached USDC balance, an on-demand orphan-sweep wake, and per-orphan re-cancel cooldowns.
        self._balance_cache: float | None = None
        self._balance_cache_at: float = 0.0
        self._orphan_sweep_wake = asyncio.Event()
        self._orphan_cancel_at: dict[str, float] = {}
        self._recent_managed_tokens: dict[str, float] = {}
        engine.live = self

    # ---- engine callbacks (live branches of _manage_inventory) ----

    def execute_taker_exit(self, pair: Any, item: Any, route: Any) -> bool:
        """Cut a stopped small position with a price-capped FOK taker on the cheaper route.

        ``route`` is the engine's chosen EmergencyCloseRoute: ``sell_current`` sells the held leg
        into its book, ``buy_opposite`` completes the set on the opposite leg. The FOK fills fully
        at a price no worse than ``touch ± slippage`` or is killed (returns False), so we never
        leave a slippery open-ended market order. On a kill the caller falls back to alerting the
        user for a manual exit.
        """
        slip = float(self.settings.reward_mm_live_auto_exit_slippage)
        tick = float(getattr(pair, "tick_size", 0.001))
        neg_risk = bool(getattr(pair, "neg_risk", False))
        try:
            if route.route == "buy_opposite":
                opposite = pair.token_b if item.token_id == pair.token_a else pair.token_a
                cap = float(route.hedge_price) + slip
                response = self.gateway.take_fok(
                    opposite, "BUY", cap, float(item.shares), tick, neg_risk=neg_risk
                )
            else:
                floor = max(0.0, float(route.exit_price) - slip)
                response = self.gateway.take_fok(
                    item.token_id, "SELL", floor, float(item.shares), tick, neg_risk=neg_risk
                )
        except Exception as exc:  # noqa: BLE001 — never let a live error crash the loop
            self._event("auto_taker_exit_error", f"{item.outcome}: {exc}", level="error")
            return False
        filled = _fok_filled(response)
        self._event(
            "auto_taker_exit",
            f"{item.outcome} {route.route} x{item.shares:g} filled={filled}",
            level="warning",
        )
        return filled

    def execute_bundle_lock(self, pair: Any, item: Any, hedge_price: float) -> bool:
        """Buy the opposite leg with a price-capped FOK to lock a profitable bundle."""
        opposite = pair.token_b if item.token_id == pair.token_a else pair.token_a
        try:
            response = self.gateway.take_fok(
                opposite, "BUY", float(hedge_price), float(item.shares), float(pair.tick_size),
                neg_risk=bool(getattr(pair, "neg_risk", False)),
            )
        except Exception as exc:  # noqa: BLE001 — never let a live error crash the loop
            self._event("bundle_lock_error", f"{item.outcome}: {exc}", level="error")
            return False
        filled = _fok_filled(response)
        self._event(
            "bundle_lock", f"{item.outcome} x{item.shares:g} @<= {hedge_price:.4f} filled={filled}"
        )
        return filled

    def alert_emergency(self, item: Any, reason: str, trigger_pnl: float | None) -> None:
        """Cancel this market's resting orders and alert the user to exit manually."""
        pair = self.engine.pairs.get(item.condition_id)
        tokens = {item.token_id}
        if pair is not None:
            tokens |= {pair.token_a, pair.token_b}
        for (token, side), order in list(self.live_orders.items()):
            if token in tokens:
                # Keep tracking a failed cancel so the reconcile loop retries it.
                if self._safe_cancel(order.order_id):
                    self.live_orders.pop((token, side), None)
        pnl = f"{trigger_pnl:.2f}" if trigger_pnl is not None else "?"
        self._event(
            "emergency_alert",
            f"MANUAL EXIT NEEDED — {item.outcome}: {reason}, pnl≈{pnl}, "
            f"{item.shares:g} sh @ avg {item.avg_price:.4f}. Bot will NOT auto-sell at a loss.",
            level="error",
        )

    # ---- reconciliation (pure-ish, unit tested) ----

    def _pair_neg_risk(self, condition_id: str) -> bool:
        pair = self.engine.pairs.get(condition_id) or self.engine.pair_cache.get(condition_id)
        return bool(getattr(pair, "neg_risk", False)) if pair is not None else False

    def desired_quotes(self) -> dict[tuple[str, str], tuple[float, float, float, float, bool]]:
        """Desired resting orders: (token, side) -> (price, size, tick, weight, neg_risk).

        When the whole market feed has gone silent (a real WS disconnect/stall), every quote is
        dropped so reconcile cancels the resting orders instead of leaving them at prices the bot
        can no longer see move — a client-side dead-man switch (the CLOB has no server-side
        cancel-on-disconnect). Orders reappear automatically once the feed resumes. A single
        quiet-but-connected market is NOT treated as blind: the feed is shared, so its book
        simply not ticking is normal and must keep scoring.
        """
        desired: dict[tuple[str, str], tuple[float, float, float, float, bool]] = {}
        if self._feed_blind():
            return desired
        for active in self.engine.active_quotes.values():
            plan = active.plan
            if self._book_missing(plan.token_id):
                continue
            desired[(plan.token_id, plan.side)] = (
                plan.price,
                plan.size,
                plan.tick_size,
                plan.score_weight,
                self._pair_neg_risk(plan.condition_id),
            )
        return desired

    def _feed_blind(self) -> bool:
        """True when no market-WS message has arrived feed-wide for the configured window."""
        threshold = self.settings.reward_mm_live_feed_stale_seconds
        if threshold <= 0:
            return False
        last = getattr(self.engine, "last_ws_message_at", None)
        if last is None:
            return False
        return (time.time() - last) > threshold

    def _book_missing(self, token_id: str) -> bool:
        """True when we hold no book at all for ``token_id`` (no data to quote against)."""
        books = getattr(self.engine, "books", None)
        if books is None:
            return False
        return books.get(token_id) is None

    def _apply_balance_cap(
        self,
        desired: dict[tuple[str, str], tuple[float, float, float, float, bool]],
        balance: float,
    ) -> dict[tuple[str, str], tuple[float, float, float, float, bool]]:
        """Keep all SELLs; keep BUYs by descending score until resting notional hits balance."""
        cap = min(balance, self.settings.reward_mm_live_max_balance_usdc)
        kept: dict[tuple[str, str], tuple[float, float, float, float, bool]] = {}
        spent = 0.0
        buys = sorted(
            ((k, v) for k, v in desired.items() if k[1] == "BUY"),
            key=lambda kv: kv[1][3],
            reverse=True,
        )
        for key, value in desired.items():
            if key[1] == "SELL":
                kept[key] = value
        for key, value in buys:
            notional = value[0] * value[1]
            if spent + notional <= cap + 1e-9:
                kept[key] = value
                spent += notional
        return kept

    def reconcile(self, balance: float) -> None:
        desired = self._apply_balance_cap(self.desired_quotes(), balance)
        # Collect every stale order and cancel them in ONE batch call, instead of N sequential
        # round-trips that freeze the event loop on many markets (delaying the price feed and
        # post-fill hedge). Tracking is dropped ONLY for ids the CLOB confirms canceled, so a
        # still-resting order is never replaced alongside itself (balance-exhaustion guard).
        stale: list[tuple[tuple[str, str], str]] = []
        for key in list(self.live_orders):
            target = desired.get(key)
            existing = self.live_orders[key]
            tick = existing.tick or 0.001
            if target is None or abs(target[0] - existing.price) > tick / 2 or (
                abs(target[1] - existing.size) > 1e-9
            ):
                stale.append((key, existing.order_id))
        if stale:
            canceled = self._batch_cancel([order_id for _key, order_id in stale])
            for key, order_id in stale:
                if order_id in canceled:
                    self.live_orders.pop(key, None)
        to_place = [
            (token, side, price, size, tick, neg_risk)
            for (token, side), (price, size, tick, _weight, neg_risk) in desired.items()
            if (token, side) not in self.live_orders
        ]
        if not to_place:
            return
        if self.settings.reward_mm_live_batch_orders:
            self._place_batch(to_place)
        else:
            self._place_sequential(to_place)

    def _place_batch(self, to_place: list) -> None:
        """Place all new orders, split into CLOB-batch-sized chunks (<=15 each).

        Batching is preserved within each chunk (one request per <=15 orders), so simultaneous
        multi-market reprices still go together instead of queuing one order per round-trip. Only
        a reconcile touching >15 orders becomes a few consecutive batch requests. Each chunk is
        placed independently, so a failure in one does not drop the others.
        """
        for chunk in chunked(to_place, _MAX_ORDERS_PER_BATCH):
            self._place_batch_chunk(chunk)

    def _place_batch_chunk(self, to_place: list) -> None:
        """Place one <=15-order batch request, then track the ids the CLOB accepted."""
        try:
            order_ids = self.gateway.place_many(to_place)
        except Exception as exc:  # noqa: BLE001 — never let a live error crash the loop
            # Ambiguous failure: the request errored but some orders may have been accepted
            # without us recording their ids (the run-25 "88 orphans" case). Sweep now so any
            # accepted-but-untracked order is cancelled immediately, not only on the routine
            # sweep — otherwise it rests unmanaged and could be adversely filled.
            self._event("place_error", f"batch[{len(to_place)}]: {exc}", level="warning")
            self._remember_managed_tokens(token for token, *_rest in to_place)
            self.request_orphan_sweep()
            return
        if len(order_ids) != len(to_place):
            self._event(
                "place_error",
                f"batch[{len(to_place)}]: returned {len(order_ids)} order ids",
                level="warning",
            )
            self._remember_managed_tokens(token for token, *_rest in to_place)
            self.request_orphan_sweep()
            order_ids = [None] * len(to_place)
        for (token, side, price, size, tick, _neg_risk), order_id in zip(
            to_place, order_ids, strict=False
        ):
            if order_id is None:
                # Rejected/unmatched: leave it untracked. The reaper cancels any stray resting
                # order so a retry next cycle never stacks a duplicate.
                self._event("place_error", f"{token[:10]} {side}: no order id", level="warning")
                self._remember_managed_tokens([token])
                self.request_orphan_sweep()
                continue
            self._remember_placed_order(str(order_id))
            self.live_orders[(token, side)] = LiveOrder(str(order_id), price, size, tick, size)

    def _place_sequential(self, to_place: list) -> None:
        """Fallback: place orders one per request (reward_mm_live_batch_orders=False)."""
        for token, side, price, size, tick, neg_risk in to_place:
            try:
                order_id = self.gateway.place_post_only(token, side, price, size, tick, neg_risk)
            except Exception as exc:  # noqa: BLE001
                self._event("place_error", f"{token[:10]} {side}: {exc}", level="warning")
                self._remember_managed_tokens([token])
                self.request_orphan_sweep()
                continue
            self._remember_placed_order(str(order_id))
            self.live_orders[(token, side)] = LiveOrder(str(order_id), price, size, tick, size)

    def _reap_orphan_orders(self) -> None:
        """Cancel resting CLOB orders on a managed token that we are no longer tracking.

        Defense-in-depth behind the confirmed-cancel logic: if an order ever escapes
        ``live_orders`` while still resting (e.g. a place whose response we failed to
        record, or leftovers from a crashed cycle), it keeps reserving collateral and
        would eventually exhaust the balance. The sweep is scoped to tokens the bot is
        currently quoting so it never touches unrelated orders on the account.
        """
        try:
            resting = self.gateway.open_orders()
        except Exception as exc:  # noqa: BLE001 — never let a live error crash the loop
            self._event("open_orders_error", str(exc), level="warning")
            return
        tracked_ids = {order.order_id for order in self.live_orders.values()}
        managed_tokens = {token for token, _side in self.live_orders}
        managed_tokens |= {token for token, _side in self.desired_quotes()}
        managed_tokens |= self._recent_tokens()
        now = time.monotonic()
        cooldown = self.settings.reward_mm_live_orphan_recancel_cooldown_seconds
        if cooldown > 0:
            for oid in [o for o, t in self._orphan_cancel_at.items() if now - t > cooldown]:
                self._orphan_cancel_at.pop(oid, None)
        for order_id, token_id in resting:
            if token_id not in managed_tokens or order_id in tracked_ids:
                continue
            if cooldown > 0 and now - self._orphan_cancel_at.get(order_id, -1e9) < cooldown:
                # Recently attempted (e.g. a cancel that hasn't taken effect yet): don't hammer.
                continue
            self._orphan_cancel_at[order_id] = now
            if self._safe_cancel(order_id):
                self._event(
                    "orphan_cancelled",
                    f"{order_id[:12]} {token_id[:10]}: untracked resting order reaped",
                    level="warning",
                )

    # ---- user-channel fills ----

    def on_user_event(self, payload: dict[str, Any]) -> None:
        fills = parse_maker_fills(payload)
        known_event = any(
            (fill.trade_id, fill.order_id) in self._applied_fills
            or self._locate_tracked_order(fill.order_id) is not None
            or fill.order_id in self._placed_order_ids
            for fill in fills
        )
        for fill in fills:
            if fill.status == "FAILED":
                # An on-chain settlement reverted a match we already booked. Never auto-reverse
                # a real position from the bot — alert the operator to reconcile by hand. Checked
                # before order lookup since the matched order was popped when first booked.
                if (fill.trade_id, fill.order_id) in self._applied_fills:
                    self._event(
                        "maker_fill_failed",
                        f"MANUAL RECONCILE — booked fill reverted: order {fill.order_id[:12]} "
                        f"{fill.token_id[:10]} x{fill.size:g} @ {fill.price:.4f}",
                        level="error",
                    )
                continue
            if (fill.trade_id, fill.order_id) in self._applied_fills:
                continue
            located = self._locate_tracked_order(fill.order_id)
            if located is None:
                if fill.order_id in self._placed_order_ids:
                    # Our own order, dropped from live tracking but filled during its cancel race
                    # (the CLOB confirmed the cancel after a taker had already matched it). Our
                    # maker side is the opposite of the taker's; recover the fill against the
                    # engine's pending-cancel quote — the real filled order, booked at the correct
                    # price — never an unrelated resting quote. If it cannot be booked now, halt so
                    # the wallet-adoption path manages the real position safely.
                    our_side = _maker_side_from_taker(fill.taker_side)
                    if our_side is not None and self.engine.apply_live_fill(
                        fill.token_id,
                        our_side,
                        fill.price,
                        float(fill.size),
                        size_shares=self.engine.size_shares,
                        pending_only=True,
                    ):
                        self._mark_fill_applied(fill.trade_id, fill.order_id)
                        self._event(
                            "maker_fill_recovered",
                            f"{our_side} {fill.size:g} @ {fill.price:.4f} token "
                            f"{fill.token_id[:10]} order {fill.order_id[:12]} (untracked, booked)",
                            level="warning",
                        )
                        continue
                    self._halt_on_tracking_risk(
                        "maker_fill_untracked",
                        f"{fill.token_id[:10]} x{fill.size:g} @ {fill.price:.4f} "
                        f"order {fill.order_id[:12]} status={fill.status}",
                    )
                    return
                if known_event:
                    # A maker order we did not place, inside an event that IS otherwise ours — not
                    # our position, so skip it rather than halting.
                    continue
                # Truly unrecognised: a fill on an id we never placed and nothing in this event is
                # ours. Too ambiguous to continue unattended — halt and sync wallet positions.
                self._halt_on_tracking_risk(
                    "maker_fill_untracked",
                    f"{fill.token_id[:10]} x{fill.size:g} @ {fill.price:.4f} "
                    f"order {fill.order_id[:12]} status={fill.status}",
                )
                return
            self._mark_fill_applied(fill.trade_id, fill.order_id)
            token, side, order = located
            remaining_before = float(getattr(order, "remaining", None) or order.size)
            matched_size = min(float(fill.size), remaining_before)
            matched = self.engine.apply_live_fill(
                fill.token_id,
                side,
                fill.price,
                matched_size,
                # The run's size (what the operator picked), not the settings default: this
                # value re-plans the replacement quote after the fill.
                size_shares=self.engine.size_shares,
            )
            if not matched:
                self._halt_on_tracking_risk(
                    "live_fill_unmatched",
                    f"{fill.token_id[:10]} {side} x{matched_size:g} @ {fill.price:.4f} "
                    f"order {fill.order_id[:12]} was tracked live but not matched in engine",
                )
                continue
            remaining = max(0.0, remaining_before - matched_size)
            key = (token, side)
            if remaining <= 1e-9:
                self.live_orders.pop(key, None)
                continue
            order.remaining = remaining
            if self._safe_cancel(order.order_id):
                self.live_orders.pop(key, None)
            else:
                self._halt_on_tracking_risk(
                    "partial_fill_cancel_failed",
                    f"{token[:10]} {side}: filled {matched_size:g}, "
                    f"could not confirm cancel for remaining {remaining:g}",
                )

    def _locate_tracked_order(self, order_id: str) -> tuple[str, str, LiveOrder] | None:
        """Return the tracked resting order we placed with ``order_id``."""
        for (token, side), order in self.live_orders.items():
            if order.order_id == order_id:
                return token, side, order
        return None

    def _mark_fill_applied(self, trade_id: str, order_id: str) -> bool:
        """Record a (trade_id, order_id) fill; return False if already applied (a dup)."""
        key = (trade_id, order_id)
        if key in self._applied_fills:
            return False
        self._applied_fills.add(key)
        self._applied_fill_keys.append(key)
        if len(self._applied_fill_keys) > 20_000:
            self._applied_fills.discard(self._applied_fill_keys.popleft())
        return True

    def _remember_placed_order(self, order_id: str) -> None:
        """Record an exchange order id we placed, so a later untracked fill on it is known ours."""
        if order_id in self._placed_order_ids:
            return
        self._placed_order_ids.add(order_id)
        self._placed_order_id_queue.append(order_id)
        if len(self._placed_order_id_queue) > 20_000:
            self._placed_order_ids.discard(self._placed_order_id_queue.popleft())

    # ---- async glue (integration; validated live, not in unit tests) ----

    async def run(
        self,
        markets: int,
        *,
        size_shares: float,
        max_loss_usdc: float | None = None,
        max_position_shares: float | None = None,
        max_exposure_usdc: float | None = None,
        seconds: float | None = None,
    ) -> dict[str, Any]:
        await self.gateway.start()
        if not self._safe_cancel_all():  # clear any orphan orders from a previous run
            raise RuntimeError("initial cancel_all failed; refusing to start live orders")
        api_key, secret, passphrase = self.gateway.api_creds()
        from polybot.feed.user_ws import UserWebSocket

        user_ws = UserWebSocket(self.settings, api_key, secret, passphrase)
        engine_task = asyncio.create_task(
            self.engine.run(
                markets,
                size_shares=size_shares,
                max_loss_usdc=max_loss_usdc,
                max_position_shares=max_position_shares,
                max_exposure_usdc=max_exposure_usdc,
                seconds=seconds,
            )
        )
        helpers = [
            asyncio.create_task(self._reconcile_loop()),
            asyncio.create_task(self._orphan_sweep_loop()),
            asyncio.create_task(self._user_ws_loop(user_ws)),
            asyncio.create_task(self._killswitch_loop()),
            asyncio.create_task(self._position_sync_loop()),
        ]
        try:
            return await engine_task  # type: ignore[no-any-return]
        finally:
            self._stop.set()
            for task in helpers:
                task.cancel()
            for task in helpers:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                    await asyncio.wait_for(task, timeout=2.0)
            self._safe_cancel_all()

    def request_reconcile(self, *, urgent: bool = False) -> None:
        """Wake the reconcile loop now (called by the engine on an adverse pull or a fill).

        ``urgent`` marks a safety trigger so the loop bypasses its inter-reconcile floor and
        cancels the endangered order immediately. The flag is sticky until the loop consumes it,
        so it is never lost if several wakes coalesce before the loop runs.
        """
        if urgent:
            self._urgent_reconcile = True
        self._reconcile_wake.set()

    def request_orphan_sweep(self) -> None:
        """Wake the orphan-sweep loop now (e.g. after an ambiguous batch-place failure)."""
        self._orphan_sweep_wake.set()

    def _cached_balance(self) -> float:
        """USDC balance, cached briefly so the fast reconcile loop isn't a balance-GET per tick.

        Balance only changes on our own fills/placements; a few-seconds-stale value for the
        resting-notional cap is safe (over-cap orders are rejected by the CLOB; under-cap simply
        self-corrects on the next refresh). Reading every event-driven reconcile would otherwise
        risk the 200/10s balance rate limit and block the event loop on each tick.
        """
        ttl = self.settings.reward_mm_live_balance_cache_seconds
        now = time.monotonic()
        if ttl > 0 and self._balance_cache is not None and now - self._balance_cache_at < ttl:
            return self._balance_cache
        self._balance_cache = self.gateway.usdc_balance()
        self._balance_cache_at = now
        return self._balance_cache

    async def _reconcile_loop(self) -> None:
        interval = self.settings.reward_mm_live_reconcile_seconds
        floor = max(0.0, self.settings.reward_mm_live_min_reconcile_seconds)
        last = 0.0
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._reconcile_wake.wait(), timeout=interval)
            self._reconcile_wake.clear()
            urgent = self._urgent_reconcile
            self._urgent_reconcile = False
            # Floor between routine reconciles so a wake burst stays well under the order/cancel
            # rate budget. A safety trigger (adverse pull / fill) bypasses the floor so the
            # endangered order is cancelled with minimal latency.
            if not urgent:
                remaining = floor - (time.monotonic() - last)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            last = time.monotonic()
            try:
                # Orphan reaping now runs on its own slow loop (and on demand after a batch
                # error), keeping the hot path to one cached-balance read + the diff/place.
                self.reconcile(self._cached_balance())
            except Exception as exc:  # noqa: BLE001
                self._event("reconcile_error", str(exc), level="warning")

    async def _orphan_sweep_loop(self) -> None:
        """Reap untracked resting orders on a slow cadence, or immediately when woken.

        Decoupled from the reconcile hot path: the blocking ``open_orders()`` round-trip runs at
        most every ``reward_mm_live_orphan_sweep_seconds`` (or right after an ambiguous batch
        place), instead of on every — now sub-second — reconcile tick.
        """
        interval = self.settings.reward_mm_live_orphan_sweep_seconds
        if interval <= 0:
            return
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._orphan_sweep_wake.wait(), timeout=interval)
            self._orphan_sweep_wake.clear()
            try:
                self._reap_orphan_orders()
            except Exception as exc:  # noqa: BLE001
                self._event("orphan_sweep_error", str(exc), level="warning")

    async def _position_sync_loop(self) -> None:
        """Periodically adopt real wallet positions the bot under-tracks (dropped fills)."""
        interval = self.settings.reward_mm_live_position_sync_seconds
        if interval <= 0:
            return
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            try:
                positions = self.gateway.positions()
                adopted = self.engine.reconcile_wallet_positions(positions)
                if adopted:
                    self._event(
                        "position_sync", f"adopted {adopted} untracked wallet position(s)"
                    )
            except Exception as exc:  # noqa: BLE001 — never let a sync error crash the loop
                self._event("position_sync_error", str(exc), level="warning")

    async def _user_ws_loop(self, user_ws: Any) -> None:
        while not self._stop.is_set():
            markets = await self._wait_user_ws_markets()
            if not markets:
                return
            subscribed = set(markets)
            async for event in user_ws.stream(markets):
                if self._stop.is_set():
                    return
                current = set(self.engine.selected_pairs)
                if current and current != subscribed:
                    break
                if event.get("event_type") in {"__connected__", "__error__"}:
                    continue
                self.on_user_event(event)

    async def _killswitch_loop(self) -> None:
        while not self._stop.is_set():
            if self.risk.kill_switch_active():
                self._safe_cancel_all()
                self._event(
                    "kill_switch", "kill switch active — cancelled all live orders", "error"
                )
                self._stop.set()
                break
            await asyncio.sleep(1.0)

    # ---- helpers ----

    def _safe_cancel(self, order_id: str) -> bool:
        """Cancel one resting order; return True only if the CLOB confirmed it.

        On any failure (transient connection drop, ``order manager not ready``, ...)
        return False so the caller keeps the order in ``live_orders`` and retries on
        the next reconcile, instead of dropping a still-resting order whose collateral
        the CLOB has not released — which would let a replacement stack a second
        reservation and exhaust the wallet balance.
        """
        try:
            result = self.gateway.cancel(order_id)
            if result is False:
                self._event("cancel_unconfirmed", f"{order_id[:12]}", level="warning")
                return False
            return True
        except Exception as exc:  # noqa: BLE001 — never let a live error crash the loop
            self._event("cancel_error", f"{order_id[:12]}: {exc}", level="warning")
            return False

    def _batch_cancel(self, order_ids: list[str]) -> set[str]:
        """Cancel many orders in CLOB-batch-sized chunks (<=15 each); return the confirmed ids.

        Batching is preserved within each chunk (one request per <=15 cancels). A chunk that
        fails contributes no confirmed ids, so its orders stay tracked and are retried next
        cycle — same guard as :meth:`_safe_cancel` returning False. Chunked defensively: the
        DELETE /orders batch limit is not separately verified but 15 is safe under it.
        """
        confirmed: set[str] = set()
        for chunk in chunked(order_ids, _MAX_ORDERS_PER_BATCH):
            try:
                confirmed |= self.gateway.cancel_many(chunk)
            except Exception as exc:  # noqa: BLE001 — never let a live error crash the loop
                self._event("cancel_error", f"batch[{len(chunk)}]: {exc}", level="warning")
        return confirmed

    def _safe_cancel_all(self) -> bool:
        try:
            self.gateway.cancel_all()
        except Exception as exc:  # noqa: BLE001
            self._event("cancel_all_error", str(exc), level="error")
            return False
        self.live_orders.clear()
        return True

    def _remember_managed_tokens(self, tokens) -> None:
        now = time.monotonic()
        for token in tokens:
            if token:
                self._recent_managed_tokens[str(token)] = now

    def _recent_tokens(self) -> set[str]:
        ttl = self.settings.reward_mm_live_orphan_token_ttl_seconds
        if ttl <= 0:
            self._recent_managed_tokens.clear()
            return set()
        now = time.monotonic()
        stale = [token for token, ts in self._recent_managed_tokens.items() if now - ts > ttl]
        for token in stale:
            self._recent_managed_tokens.pop(token, None)
        return set(self._recent_managed_tokens)

    async def _wait_user_ws_markets(self) -> list[str]:
        while not self._stop.is_set():
            markets = list(self.engine.selected_pairs)
            if markets:
                return markets
            await asyncio.sleep(0.1)
        return []

    def _halt_on_tracking_risk(self, kind: str, message: str) -> None:
        self._event(kind, message, level="error")
        self._sync_positions_once()
        self.request_orphan_sweep()
        if self.settings.reward_mm_live_stop_on_untracked_fill:
            self._safe_cancel_all()
            self._stop.set()

    def _sync_positions_once(self) -> None:
        positions = getattr(self.gateway, "positions", None)
        if not callable(positions):
            return
        try:
            adopted = self.engine.reconcile_wallet_positions(positions())
        except Exception as exc:  # noqa: BLE001
            self._event("position_sync_error", str(exc), level="warning")
            return
        if adopted:
            self._event("position_sync", f"adopted {adopted} untracked wallet position(s)")

    def _event(self, kind: str, message: str, level: str = "info") -> None:
        if self.engine.run_id is not None and self.storage is not None:
            self.storage.insert_reward_event(self.engine.run_id, kind, message, level=level)

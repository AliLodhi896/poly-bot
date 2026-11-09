from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import asdict, dataclass, replace
from decimal import Decimal

import httpx

from polybot.api.clob import ClobClient
from polybot.config import Settings
from polybot.feed.books import BookStore, TradePrint
from polybot.feed.ws import MarketWebSocket, chunked
from polybot.fees import taker_fee_per_share
from polybot.rewards.models import RewardPair, RewardQuotePlan
from polybot.rewards.planner import RewardQuotePlanner, depth_mults_for_min_shares
from polybot.storage import Storage


def _is_transient_http_error(exc: httpx.HTTPError) -> bool:
    return isinstance(exc, httpx.RequestError) or (
        isinstance(exc, httpx.HTTPStatusError)
        and (exc.response.status_code == 429 or exc.response.status_code >= 500)
    )


@dataclass
class RewardMetrics:
    markets_requested: int = 0
    markets_watched: int = 0
    tokens_watched: int = 0
    books_ready: int = 0
    ws_connected: int = 0
    ws_messages: int = 0
    trade_prints: int = 0
    quotes_opened: int = 0
    quote_replacements: int = 0
    quote_cancellations: int = 0
    quote_snapshots: int = 0
    current_open_quotes: int = 0
    current_scoring_quotes: int = 0
    current_non_scoring_quotes: int = 0
    stale_open_quotes: int = 0
    risk_only_quotes: int = 0
    scoring_quote_seconds: float = 0.0
    estimated_reward_usdc: float = 0.0
    liquidity_reward_conservative_usdc: float = 0.0
    liquidity_reward_base_usdc: float = 0.0
    liquidity_reward_optimistic_usdc: float = 0.0
    liquidity_q_one_seconds: float = 0.0
    liquidity_q_two_seconds: float = 0.0
    liquidity_q_min_seconds: float = 0.0
    score_weight: float = 0.0
    would_have_fills: int = 0
    fill_before_cancel: int = 0
    cancel_pending_quotes: int = 0
    cancel_acknowledged_quotes: int = 0
    cancel_latency_seconds_total: float = 0.0
    maker_fee_equivalent_usdc: float = 0.0
    maker_rebate_conservative_usdc: float = 0.0
    maker_rebate_base_usdc: float = 0.0
    maker_rebate_optimistic_usdc: float = 0.0
    inventory_positions: int = 0
    inventory_exposure: float = 0.0
    inventory_unrealized_pnl: float = 0.0
    locked_bundles: int = 0
    locked_bundle_pnl: float = 0.0
    passive_exits: int = 0
    passive_exit_pnl: float = 0.0
    emergency_exits: int = 0
    emergency_exit_pnl: float = 0.0
    stop_slippage_usdc: float = 0.0
    reward_adjusted_net_pnl: float = 0.0
    reward_net_conservative_usdc: float = 0.0
    reward_net_base_usdc: float = 0.0
    reward_net_optimistic_usdc: float = 0.0
    matched_bundle_positions: int = 0
    matched_bundle_shares: float = 0.0
    matched_bundle_pnl: float = 0.0
    unhedged_shares: float = 0.0
    unhedged_pnl: float = 0.0
    inventory_state_errors: int = 0
    cooldown_markets: int = 0
    cooldown_entry_blocks: int = 0
    halted_markets: int = 0
    positions_adopted: int = 0
    live_readiness_passed: int = 0
    live_readiness_blockers: int = 0
    live_readiness_fill_before_cancel_rate: float = 0.0
    market_refresh_failures: int = 0
    market_data_cycle_failures: int = 0
    markets_rejected_volatility: int = 0
    markets_rejected_depth: int = 0
    markets_rejected_extreme: int = 0
    game_paused_markets: int = 0
    auto_taker_exits: int = 0
    auto_taker_exit_pnl: float = 0.0
    competitor_q_avg: float = 0.0
    snapshots_pruned: int = 0
    unstable_markets: int = 0
    runtime_volatile_markets: int = 0
    warmup_blocked_markets: int = 0
    markets_rejected_resolution: int = 0
    markets_rejected_category: int = 0
    markets_rejected_low_payout: int = 0
    markets_rejected_min_size: int = 0
    markets_rejected_thin_for_size: int = 0
    markets_paying_reward: int = 0
    trending_markets: int = 0


@dataclass(frozen=True)
class RewardScenarioEstimate:
    conservative: float
    base: float
    optimistic: float
    base_fee_equivalent: float = 0.0


@dataclass
class ActiveRewardQuote:
    quote_uid: str
    plan: RewardQuotePlan
    opened_at: float
    queue_ahead: float
    last_eval_at: float
    last_snapshot_at: float = 0.0
    last_record_sig: tuple | None = None
    effective_scoring: bool = False
    lifecycle_state: str = "open"
    cancel_requested_at: float | None = None
    cancel_ack_at: float | None = None
    cancel_reason: str = ""
    cancel_final_status: str = "cancelled"


@dataclass(frozen=True)
class EmergencyCloseRoute:
    route: str
    realized_pnl: float
    exit_price: float | None = None
    hedge_price: float | None = None
    bundle_cost: float | None = None


@dataclass
class RewardInventory:
    condition_id: str
    token_id: str
    outcome: str
    inventory_uid: str
    leg: str
    opened_at: float
    state: str = "open_inventory"
    shares: float = 0.0
    total_cost: float = 0.0
    mark_price: float | None = None
    reward_credit_usdc: float = 0.0
    best_hedge_price: float | None = None
    best_hedge_bundle_cost: float | None = None
    best_hedge_pnl: float | None = None
    passive_exit_quote_uid: str | None = None
    passive_exit_price: float | None = None
    closed_at: float | None = None
    close_mode: str | None = None
    closed_pnl: float | None = None
    reward_adjusted_pnl: float | None = None
    last_mark_at: float = 0.0

    @property
    def avg_price(self) -> float:
        return self.total_cost / self.shares if self.shares > 0 else 0.0

    @property
    def unrealized_pnl(self) -> float | None:
        if self.mark_price is None:
            return None
        return (self.mark_price - self.avg_price) * self.shares

    @property
    def liquidation_pnl(self) -> float | None:
        return self.unrealized_pnl


class RewardMMEngine:
    """Paper-only liquidity-reward market-making research engine."""

    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage
        self.metrics = RewardMetrics()
        self.pairs: dict[str, RewardPair] = {}
        self.books: BookStore | None = None
        self.run_id: int | None = None
        self.planner = RewardQuotePlanner(
            settings.reward_mm_quote_distance_fraction,
            depth_based=settings.reward_mm_depth_based_placement,
        )
        self.selected_pairs: dict[str, RewardPair] = {}
        self.active_quotes: dict[tuple[str, str, str], ActiveRewardQuote] = {}
        self.inventory: dict[tuple[str, str], RewardInventory] = {}
        self.pair_cache: dict[str, RewardPair] = {}
        self.market_last_eval_at: dict[str, float] = {}
        self.market_reward_credit_available: dict[str, float] = {}
        self.market_midpoint_marks: dict[str, tuple[float, float, float]] = {}
        self.market_cooldowns: dict[str, float] = {}
        self.market_competitor_score: dict[str, float] = {}
        self.market_midpoint_ema: dict[str, tuple[float, float]] = {}
        self.market_midpoint_window: dict[str, list[tuple[float, float, float]]] = {}
        self.market_first_seen: dict[str, float] = {}
        # Markets retired by stop-loss when reward_mm_halt_market_on_stop_loss is on.
        # In-memory only, so a bot restart clears it and re-enables the market.
        self.halted_markets: set[str] = set()
        # Markets that have taken a fill: blocked from NEW entries for the rest of the run while
        # the post-fill management (race/stop/auto-exit) keeps running on the open position.
        # In-memory only, so a restart re-enables fresh entries. See _retire_market_after_fill.
        self.markets_retired_after_fill: set[str] = set()
        # Last time we acted on a stop-loss (auto-exit attempt / alert) per market, to throttle
        # the per-tick retry storm when a just-filled position's shares are not yet settled.
        self._last_stop_action_at: dict[str, float] = {}
        # Off-universe wallet positions already alerted, so we don't repeat the warning.
        self._alerted_untracked: set[str] = set()
        # Pinned (whitelist) markets dropped during discovery, with the reason — logged only when
        # the set changes, so a persistently-dropped pin does not spam the event log every refresh.
        self._whitelist_dropped: dict[str, str] = {}
        self._last_prune_at: float = 0.0
        # Set by LiveRewardCoordinator in live mode; None ⇒ paper (simulated fills).
        self.live: object | None = None
        # Wall-clock of the most recent market-WS message across the whole feed (any token).
        # The live reconcile uses this as the "are we blind?" signal: the feed is shared, so a
        # quiet single market still receives messages from the others — only a real disconnect
        # goes fully silent. Initialized to now so we are not considered blind before the feed
        # has had a chance to start.
        self.last_ws_message_at: float = time.time()
        self.pending_cancel_quotes: dict[str, ActiveRewardQuote] = {}
        self.last_live_readiness_blockers: tuple[str, ...] = ()
        self.quote_sequence = 0
        self.inventory_sequence = 0
        self.max_loss_usdc = settings.reward_mm_max_loss_usdc
        self.max_position_shares = settings.reward_mm_max_position_shares
        self.max_exposure_usdc = settings.reward_mm_max_exposure_usdc
        # The run's quote size, set by :meth:`run` from the operator's choice (the UI's position
        # size / ``--size``). Anything outside the run loop — the reward projection, the live
        # coordinator re-planning a quote after a fill — must read THIS, not the settings
        # default, or it silently works at a size the operator did not pick.
        self.size_shares = settings.reward_mm_size_shares

    async def run(
        self,
        markets: int,
        *,
        size_shares: float,
        max_loss_usdc: float | None = None,
        max_position_shares: float | None = None,
        max_exposure_usdc: float | None = None,
        seconds: float | None = None,
    ) -> dict[str, float | int]:
        self.storage.init()
        self.last_ws_message_at = time.time()
        self.metrics.markets_requested = markets
        self.size_shares = size_shares
        self.max_loss_usdc = (
            max_loss_usdc if max_loss_usdc is not None else self.settings.reward_mm_max_loss_usdc
        )
        self.max_position_shares = (
            max_position_shares
            if max_position_shares is not None
            else self.settings.reward_mm_max_position_shares
        )
        self.max_exposure_usdc = (
            max_exposure_usdc
            if max_exposure_usdc is not None
            else self.settings.reward_mm_max_exposure_usdc
        )
        self.run_id = self.storage.create_reward_run(
            markets,
            size_shares,
            {
                "paper_only": True,
                "strategy": "reward_mm",
                "quote_distance_fraction": self.settings.reward_mm_quote_distance_fraction,
                "assumed_reward_share": self.settings.reward_mm_assumed_reward_share,
                "single_sided_score_factor": self.settings.reward_mm_single_sided_score_factor,
                "liquidity_share_cap_conservative": (
                    self.settings.reward_mm_liquidity_share_cap_conservative
                ),
                "liquidity_share_cap_base": self.settings.reward_mm_liquidity_share_cap_base,
                "liquidity_share_cap_optimistic": (
                    self.settings.reward_mm_liquidity_share_cap_optimistic
                ),
                "liquidity_competitor_score_conservative": (
                    self.settings.reward_mm_liquidity_competitor_score_conservative
                ),
                "liquidity_competitor_score_base": (
                    self.settings.reward_mm_liquidity_competitor_score_base
                ),
                "liquidity_competitor_score_optimistic": (
                    self.settings.reward_mm_liquidity_competitor_score_optimistic
                ),
                "maker_rebate_rate_conservative": (
                    self.settings.reward_mm_maker_rebate_rate_conservative
                ),
                "maker_rebate_rate_base": self.settings.reward_mm_maker_rebate_rate_base,
                "maker_rebate_rate_optimistic": (
                    self.settings.reward_mm_maker_rebate_rate_optimistic
                ),
                "cancel_latency_ms": self.settings.reward_mm_cancel_latency_ms,
                "live_shadow_enabled": self.settings.reward_mm_live_shadow_enabled,
                "max_loss_usdc": self.max_loss_usdc,
                "max_position_shares": self.max_position_shares,
                "max_exposure_usdc": self.max_exposure_usdc,
                "gap_guard_move": self.settings.reward_mm_gap_guard_move,
                "gap_guard_window_seconds": (
                    self.settings.reward_mm_gap_guard_window_seconds
                ),
                "gap_guard_cooldown_seconds": (
                    self.settings.reward_mm_gap_guard_cooldown_seconds
                ),
                "snapshot_interval_seconds": (
                    self.settings.reward_mm_snapshot_interval_seconds
                ),
                "max_one_day_change": self.settings.reward_mm_max_one_day_change,
                "min_top_depth_shares": self.settings.reward_mm_min_top_depth_shares,
                "min_midpoint": self.settings.reward_mm_min_midpoint,
                "priority_vol_k": self.settings.reward_mm_priority_vol_k,
                "game_pause_enabled": self.settings.reward_mm_game_pause_enabled,
                "game_pause_pre_seconds": self.settings.reward_mm_game_pause_pre_seconds,
                "game_pause_post_seconds": self.settings.reward_mm_game_pause_post_seconds,
                "size_vol_k": self.settings.reward_mm_size_vol_k,
                "distance_vol_widen_k": self.settings.reward_mm_distance_vol_widen_k,
                "scratch_loss_per_share": self.settings.reward_mm_scratch_loss_per_share,
                "max_net_delta_shares": self.settings.reward_mm_max_net_delta_shares,
                "complete_leg_distance_fraction": (
                    self.settings.reward_mm_complete_leg_distance_fraction
                ),
                "competitor_from_book": self.settings.reward_mm_competitor_from_book,
                "snapshot_retention_seconds": (
                    self.settings.reward_mm_snapshot_retention_seconds
                ),
                "warmup_seconds": self.settings.reward_mm_warmup_seconds,
                "midpoint_ema_alpha": self.settings.reward_mm_midpoint_ema_alpha,
                "stability_band": self.settings.reward_mm_stability_band,
                "post_close_cooldown_seconds": (
                    self.settings.reward_mm_post_close_cooldown_seconds
                ),
                "trend_window_seconds": self.settings.reward_mm_trend_window_seconds,
                "trend_drift": self.settings.reward_mm_trend_drift,
                "runtime_volatility_window_seconds": (
                    self.settings.reward_mm_runtime_volatility_window_seconds
                ),
                "runtime_volatility_range": (
                    self.settings.reward_mm_runtime_volatility_range
                ),
                "runtime_jump_window_seconds": (
                    self.settings.reward_mm_runtime_jump_window_seconds
                ),
                "runtime_jump_move": self.settings.reward_mm_runtime_jump_move,
                "runtime_history_timeout_seconds": (
                    self.settings.reward_mm_runtime_history_timeout_seconds
                ),
                "selection_mode": self.settings.reward_mm_selection_mode,
                "max_min_size_shares": self.settings.reward_mm_max_min_size_shares,
                "entry_depth_multiplier": self.settings.reward_mm_entry_depth_multiplier,
                "depth_based_placement": self.settings.reward_mm_depth_based_placement,
                "market_whitelist": sorted(self.settings.reward_mm_whitelist),
                "min_hours_to_resolution": (
                    self.settings.reward_mm_min_hours_to_resolution
                ),
                "exclude_keywords": sorted(self.settings.reward_mm_exclude_keyword_set),
                "select_min_payout_usdc": (
                    self.settings.reward_mm_select_min_payout_usdc
                ),
                "dust_size_cutoff": self.settings.reward_mm_dust_size_cutoff,
                "min_payout_usdc": self.settings.reward_mm_min_payout_usdc,
            },
        )
        self.storage.insert_reward_event(
            self.run_id,
            "started",
            f"reward-mm paper started for up to {markets} markets at {size_shares:g} shares",
        )
        deadline = time.monotonic() + seconds if seconds is not None else None
        status = "stopped"
        try:
            async with ClobClient(self.settings) as clob:
                while deadline is None or time.monotonic() < deadline:
                    try:
                        selected = await self._discover_pairs(clob, markets, size_shares)
                    except httpx.HTTPError as exc:
                        if not _is_transient_http_error(exc):
                            raise
                        self.metrics.market_refresh_failures += 1
                        self.storage.insert_reward_event(
                            self.run_id,
                            "market_refresh_error",
                            f"{type(exc).__name__}: retaining {len(self.pairs)} markets",
                            level="warning",
                        )
                        self._publish_metrics()
                        selected = list(self.selected_pairs.values()) or list(
                            self.pairs.values()
                        )
                    if selected:
                        self._set_selected_pairs(selected)
                        self.storage.upsert_reward_markets(
                            self.run_id, list(self.selected_pairs.values())
                        )
                        self.metrics.markets_watched = len(selected)
                        self.metrics.tokens_watched = len(self.pairs) * 2
                        self.storage.insert_reward_event(
                            self.run_id,
                            "universe",
                            f"watching {len(selected)} reward markets",
                        )
                    remaining = (
                        None if deadline is None else max(0.0, deadline - time.monotonic())
                    )
                    duration = self.settings.reward_mm_refresh_seconds
                    if remaining is not None:
                        duration = min(duration, remaining)
                    if not self.pairs or duration <= 0:
                        await asyncio.sleep(min(duration or 0.1, self.settings.heartbeat_seconds))
                    else:
                        try:
                            await self._collect_cycle(clob, duration, size_shares)
                        except httpx.HTTPError as exc:
                            if not _is_transient_http_error(exc):
                                raise
                            self.metrics.market_data_cycle_failures += 1
                            self.storage.insert_reward_event(
                                self.run_id,
                                "market_data_cycle_error",
                                f"{type(exc).__name__}: retrying reward market data",
                                level="warning",
                            )
                            self._publish_metrics()
                            await asyncio.sleep(self.settings.heartbeat_seconds)
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            status = "failed"
            self.storage.insert_reward_event(
                self.run_id,
                "failure",
                str(exc),
                level="error",
                payload={"type": type(exc).__name__},
            )
            raise
        finally:
            self._cancel_all_quotes("run_stopped")
            self._publish_metrics()
            if self.run_id is not None:
                self.storage.finish_reward_run(self.run_id, status)
        return asdict(self.metrics)

    def _set_selected_pairs(self, selected: list[RewardPair]) -> None:
        now = time.time()
        self.selected_pairs = {pair.condition_id: pair for pair in selected}
        self.pair_cache.update(self.selected_pairs)
        watched = dict(self.selected_pairs)
        for condition_id in self._open_inventory_condition_ids():
            pair = (
                self.selected_pairs.get(condition_id)
                or self.pair_cache.get(condition_id)
                or self.pairs.get(condition_id)
            )
            if pair is not None:
                watched[condition_id] = pair
                self.pair_cache[condition_id] = pair
        self.pairs = watched
        self._cancel_stale_quotes(now)

    async def _discover_pairs(
        self, clob: ClobClient, target: int, size_shares: float = 0.0
    ) -> list[RewardPair]:
        whitelist = self.settings.reward_mm_whitelist
        if whitelist:
            # Pinned markets are human-curated: page the pool deep enough to find every pin
            # regardless of its reward-rate rank. A single top-500 page silently drops a low-rate
            # pin (the run-49 disappearing-market bug).
            raw_markets = await self._fetch_whitelisted_markets(clob, whitelist)
        else:
            raw_markets, _cursor = await clob.get_rewards_markets(
                limit=max(1, min(target * 2, 500)),
                order_by="rate_per_day",
                position="DESC",
            )
        semaphore = asyncio.Semaphore(20)

        async def hydrate(market: object) -> RewardPair | None:
            condition_id = getattr(market, "condition_id", "")
            if not condition_id:
                return None
            async with semaphore:
                info = await clob.get_clob_market_info(condition_id)
            pair = RewardPair.from_market(
                market,  # type: ignore[arg-type]
                tick_size=info.tick_size,
                min_order_size=info.min_order_size,
                minimum_order_age_seconds=info.minimum_order_age_seconds,
                fee_rate=info.fee_info.rate,
                fees_enabled=info.fee_info.enabled,
                neg_risk=info.neg_risk,
            )
            if (
                pair is None
                or pair.rewards_rate_per_day <= 0
                or pair.rewards_max_spread is None
                or pair.rewards_max_spread <= 0
            ):
                return None
            return pair

        hydrated = await asyncio.gather(*(hydrate(market) for market in raw_markets))
        candidates = [pair for pair in hydrated if pair is not None]
        token_to_condition = {
            token: pair.condition_id
            for pair in candidates
            for token in (pair.token_a, pair.token_b)
        }
        if not token_to_condition:
            return []
        books = BookStore(token_to_condition)
        books.load_books(await clob.get_order_books(list(token_to_condition)))
        scored: list[tuple[float, RewardPair]] = []
        for pair in candidates:
            book_a = books.get(pair.token_a)
            book_b = books.get(pair.token_b)
            if (
                book_a is None
                or book_b is None
                or book_a.midpoint() is None
                or book_b.midpoint() is None
            ):
                continue
            # Pinned markets are human-curated: trust them, skip the auto toxicity/payout filters.
            if not whitelist:
                ok, reason = self._market_passes_filters(
                    pair, book_a, book_b, size_shares
                )
                if not ok:
                    if reason == "volatility":
                        self.metrics.markets_rejected_volatility += 1
                    elif reason == "depth":
                        self.metrics.markets_rejected_depth += 1
                    elif reason == "extreme":
                        self.metrics.markets_rejected_extreme += 1
                    elif reason == "resolution":
                        self.metrics.markets_rejected_resolution += 1
                    elif reason == "category":
                        self.metrics.markets_rejected_category += 1
                    elif reason == "low_payout":
                        self.metrics.markets_rejected_low_payout += 1
                    elif reason == "min_size":
                        self.metrics.markets_rejected_min_size += 1
                    elif reason == "thin_for_size":
                        self.metrics.markets_rejected_thin_for_size += 1
                    continue
            competitor = self._book_competitor_q(pair, books)
            if not whitelist:
                floor = self.settings.reward_mm_select_min_payout_usdc
                if floor > 0 and self._projected_daily_reward(pair, competitor) < floor:
                    self.metrics.markets_rejected_low_payout += 1
                    continue
            scored.append((self._market_priority(pair, competitor), pair))
        scored.sort(key=lambda item: item[0], reverse=True)
        keep = self._selection_count(target, whitelist)
        result = [pair for _priority, pair in scored[:keep]]
        if whitelist:
            self._log_whitelist_drops(whitelist, raw_markets, candidates, result)
        return result

    async def _fetch_whitelisted_markets(
        self, clob: ClobClient, whitelist: set[str]
    ) -> list[object]:
        """Page the reward pool until every pinned market is found (or the pool is exhausted).

        ``get_rewards_markets`` returns one ranked page (≤500). A low-rate pin sits beyond the
        first page and was silently lost; here we follow the cursor until all pins are located.
        """
        found: dict[str, object] = {}
        cursor: str | None = None
        cap = max(500, int(self.settings.reward_mm_selector_pool_limit))
        fetched = 0
        while fetched < cap:
            page, cursor = await clob.get_rewards_markets(
                limit=min(500, cap - fetched),
                order_by="rate_per_day",
                position="DESC",
                next_cursor=cursor,
            )
            if not page:
                break
            fetched += len(page)
            for market in page:
                cid = getattr(market, "condition_id", "")
                if cid in whitelist or getattr(market, "slug", None) in whitelist:
                    found[cid] = market
            if len(found) >= len(whitelist) or not cursor or cursor in ("LTE=", ""):
                break
        return list(found.values())

    def _log_whitelist_drops(
        self,
        whitelist: set[str],
        raw_markets: list[object],
        candidates: list[RewardPair],
        result: list[RewardPair],
    ) -> None:
        """Record why a pinned market did not make it onto the dashboard, so a vanished pin is no
        longer a silent mystery. Logged only when the drop set changes (no per-refresh spam)."""
        in_pool = {getattr(m, "condition_id", "") for m in raw_markets}
        in_pool |= {getattr(m, "slug", None) for m in raw_markets}
        hydrated = {pair.condition_id for pair in candidates}
        kept = {pair.condition_id for pair in result}
        reasons: dict[str, str] = {}
        for pin in whitelist:
            if pin in kept:
                continue
            if pin not in in_pool:
                reasons[pin] = "not in reward pool (rewards ended or beyond pool)"
            elif pin not in hydrated:
                reasons[pin] = "rewards missing / unfarmable metadata"
            else:
                reasons[pin] = "no two-sided book (no midpoint) this refresh"
        if reasons == self._whitelist_dropped:
            return
        recovered = set(self._whitelist_dropped) - set(reasons)
        self._whitelist_dropped = reasons
        if self.run_id is None:
            return
        for pin, reason in reasons.items():
            self.storage.insert_reward_event(
                self.run_id,
                "whitelist_dropped",
                f"pinned market {pin[:14]} not shown: {reason}",
                level="warning",
                payload={"condition_id": pin, "reason": reason},
            )
        for pin in recovered:
            self.storage.insert_reward_event(
                self.run_id,
                "whitelist_recovered",
                f"pinned market {pin[:14]} back in selection",
                payload={"condition_id": pin},
            )

    @staticmethod
    def _selection_count(target: int, whitelist: set[str]) -> int:
        """How many discovered pairs to quote.

        A pinned whitelist is explicit human curation (and, from the dashboard, real
        money on every selected market), so every pinned market is honored even if it
        exceeds the ``--markets`` target. Without a whitelist, keep the lean target.
        """
        return max(target, len(whitelist)) if whitelist else target

    def _market_passes_filters(
        self, pair: RewardPair, book_a: object, book_b: object, size_shares: float = 0.0
    ) -> tuple[bool, str]:
        """Reject toxic markets: esports, volatile, thin, near 0/1 extremes, or near resolution."""
        keywords = self.settings.reward_mm_exclude_keyword_set
        if keywords:
            haystack = f"{pair.question} {pair.event_slug or ''}".lower()
            if any(kw in haystack for kw in keywords):
                return False, "category"
        max_min_size = self.settings.reward_mm_max_min_size_shares
        if max_min_size > 0 and pair.rewards_min_size > max_min_size:
            return False, "min_size"
        min_hours = self.settings.reward_mm_min_hours_to_resolution
        if min_hours > 0 and pair.end_date is not None:
            hours_left = (pair.end_date.timestamp() - time.time()) / 3600.0
            if hours_left < min_hours:
                return False, "resolution"
        max_change = self.settings.reward_mm_max_one_day_change
        if max_change > 0 and abs(pair.one_day_price_change) > max_change:
            return False, "volatility"
        midpoint = book_a.midpoint()  # type: ignore[attr-defined]
        min_mid = self.settings.reward_mm_min_midpoint
        if midpoint is not None and min_mid > 0:
            mid = float(midpoint)
            if mid < min_mid or mid > 1.0 - min_mid:
                return False, "extreme"
        min_depth = self.settings.reward_mm_min_top_depth_shares
        if min_depth > 0:
            depth_a = float(book_a.best_bid_depth()) + float(  # type: ignore[attr-defined]
                book_a.best_ask_depth()  # type: ignore[attr-defined]
            )
            depth_b = float(book_b.best_bid_depth()) + float(  # type: ignore[attr-defined]
                book_b.best_ask_depth()  # type: ignore[attr-defined]
            )
            if min(depth_a, depth_b) < min_depth:
                return False, "depth"
        if self.settings.reward_mm_depth_based_placement and not (
            self._leg_farmable_by_depth(pair, book_a, "a", pair.outcome_a, size_shares)
            and self._leg_farmable_by_depth(pair, book_b, "b", pair.outcome_b, size_shares)
        ):
            # Depth-based mode: both legs must hold the floor buffer of shares ahead inside the
            # reward zone for our quote size. If either leg's book is too thin, the quote would
            # either sit outside the reward zone (earning nothing) or with no protection — so we
            # do not select the market at all.
            return False, "thin_for_size"
        return True, ""

    def _leg_farmable_by_depth(
        self, pair: RewardPair, book: object, leg: str, outcome: str, size_shares: float
    ) -> bool:
        """True when a depth-placed BUY for ``size_shares`` is feasible on this leg's book.

        Reuses the live placement path (``plan_buy`` in depth mode returns None when no in-zone
        tick clears the floor buffer), so selection and quoting agree by construction.
        """
        return (
            self.planner.plan_buy(
                pair=pair,
                book=book,  # type: ignore[arg-type]
                leg=leg,
                outcome=outcome,
                size_shares=size_shares,
            )
            is not None
        )

    def _projected_daily_reward(self, pair: RewardPair, competitor: float) -> float:
        """Expected daily reward to us = pool rate x our share, given competitor liquidity."""
        max_spread = pair.rewards_max_spread
        if max_spread is None or max_spread <= 0:
            return 0.0
        distance = max_spread * self.planner.quote_distance_fraction
        # At the run's own size: projecting at the settings default would report the reward for
        # a quote this run will never place.
        q_self = self.planner._distance_score(max_spread, distance) * self.size_shares
        competitor_base = max(0.0, competitor) * self.settings.reward_mm_competitor_haircut_base
        cap = self.settings.reward_mm_liquidity_share_cap_base
        if competitor_base <= 1e-12:
            share = min(1.0, cap)
        else:
            share = min(cap, q_self / (q_self + competitor_base))
        return pair.rewards_rate_per_day * share

    def _market_priority(self, pair: RewardPair, competitor: float) -> float:
        """Rank markets by the configured selection mode (default: trading activity)."""
        mode = self.settings.reward_mm_selection_mode
        if mode == "volume":
            return pair.volume_24hr
        if mode == "rate":
            return pair.rewards_rate_per_day
        k = max(0.0, self.settings.reward_mm_priority_vol_k)
        return self._projected_daily_reward(pair, competitor) / (
            1.0 + k * abs(pair.one_day_price_change)
        )

    async def _collect_cycle(
        self, clob: ClobClient, duration: float, size_shares: float
    ) -> None:
        token_to_condition = {
            token: pair.condition_id
            for pair in self.pairs.values()
            for token in (pair.token_a, pair.token_b)
        }
        tokens = list(token_to_condition)
        self.books = BookStore(token_to_condition)
        self.books.load_books(await clob.get_order_books(tokens))
        now = time.time()
        await self._refresh_history_volatility_cooldowns(clob, list(self.pairs.values()), now)
        self._settle_pending_cancels(now)
        self._cancel_stale_quotes(now)
        self.metrics.books_ready = sum(
            1
            for pair in self.pairs.values()
            for token in (pair.token_a, pair.token_b)
            if self.books.get(token) is not None
        )
        for pair in self.pairs.values():
            self._handle_pair(pair, size_shares, force_snapshot=True)
        self._publish_metrics()
        tasks = [
            asyncio.create_task(self._consume_ws(batch, size_shares))
            for batch in chunked(tokens, self.settings.ws_batch_size)
        ]
        clock = asyncio.create_task(self._decision_clock(size_shares))
        try:
            await asyncio.sleep(duration)
        finally:
            for task in [*tasks, clock]:
                task.cancel()
            for task in [*tasks, clock]:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(task, timeout=2.0)

    async def _refresh_history_volatility_cooldowns(
        self, clob: ClobClient, pairs: list[RewardPair], now: float
    ) -> None:
        long_window = self.settings.reward_mm_runtime_volatility_window_seconds
        range_limit = self.settings.reward_mm_runtime_volatility_range
        jump_window = self.settings.reward_mm_runtime_jump_window_seconds
        jump_limit = self.settings.reward_mm_runtime_jump_move
        if (long_window <= 0 or range_limit <= 0) and (jump_window <= 0 or jump_limit <= 0):
            return
        timeout = self.settings.reward_mm_runtime_history_timeout_seconds
        if timeout <= 0:
            return
        semaphore = asyncio.Semaphore(8)

        async def check(pair: RewardPair) -> None:
            async with semaphore:
                history = await clob.price_history(pair.token_a, interval="6h", fidelity=1)
            trigger = self._history_volatility_trigger(history, now)
            if trigger is None:
                return
            label, move, window_seconds = trigger
            self._set_cooldown(
                pair.condition_id,
                now,
                reason=f"recent price {label} {move:.4f}/{window_seconds:g}s",
                move=move,
            )

        tasks = [asyncio.create_task(check(pair)) for pair in pairs]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await task
            if self.run_id is not None:
                self.storage.insert_reward_event(
                    self.run_id,
                    "history_volatility_timeout",
                    f"skipped slow prices-history volatility guard after {timeout:g}s",
                    level="warning",
                )

    def _history_volatility_trigger(
        self, history: list[tuple[float, float]], now: float
    ) -> tuple[str, float, float] | None:
        long_window = self.settings.reward_mm_runtime_volatility_window_seconds
        range_limit = self.settings.reward_mm_runtime_volatility_range
        if long_window > 0 and range_limit > 0:
            price_range = self._history_window_range(history, now, long_window)
            if price_range >= range_limit:
                return "range", price_range, long_window

        jump_window = self.settings.reward_mm_runtime_jump_window_seconds
        jump_limit = self.settings.reward_mm_runtime_jump_move
        if jump_window > 0 and jump_limit > 0:
            jump = self._history_window_range(history, now, jump_window)
            if jump >= jump_limit:
                return "jump", jump, jump_window
        return None

    @staticmethod
    def _history_window_range(
        history: list[tuple[float, float]], now: float, window_seconds: float
    ) -> float:
        if window_seconds <= 0:
            return 0.0
        cutoff = now - window_seconds
        prices = [price for ts, price in history if cutoff <= ts <= now + 60.0]
        if len(prices) < 2:
            return 0.0
        return max(prices) - min(prices)

    async def _consume_ws(self, tokens: list[str], size_shares: float) -> None:
        assert self.books is not None
        async for payload in MarketWebSocket(self.settings).stream(tokens):
            event_type = payload.get("event_type")
            if event_type == "__connected__":
                self.metrics.ws_connected += 1
                # Treat (re)connection as feed activity so a just-resumed feed is not
                # immediately judged blind before its first book snapshot arrives.
                self.last_ws_message_at = time.time()
                self._publish_metrics()
                continue
            if event_type == "__error__":
                # Do NOT refresh last_ws_message_at here: an error means no data is flowing,
                # which is exactly the blindness the feed-staleness guard must detect.
                self.storage.insert_reward_event(
                    self.run_id, "ws_error", str(payload.get("message")), level="warning"
                )
                continue
            self.metrics.ws_messages += 1
            self.last_ws_message_at = time.time()
            affected, trades = self.books.apply_ws_message(payload)
            for trade in trades:
                self.metrics.trade_prints += 1
                if self.live is None:
                    # Live mode fills come from the authenticated user channel, not public prints.
                    self._apply_trade(trade, time.time(), size_shares=size_shares)
            for condition_id in self.books.affected_conditions(affected):
                pair = self.pairs.get(condition_id)
                if pair is not None:
                    self._handle_pair(pair, size_shares)

    async def _decision_clock(self, size_shares: float) -> None:
        while True:
            await asyncio.sleep(self.settings.reward_mm_snapshot_interval_seconds)
            now = time.time()
            self._settle_pending_cancels(now)
            for pair in self.pairs.values():
                self._handle_pair(pair, size_shares, force_snapshot=True)
            self._publish_metrics()
            self._prune_snapshots(now)

    def _prune_snapshots(self, now: float) -> None:
        retention = self.settings.reward_mm_snapshot_retention_seconds
        if retention <= 0 or self.run_id is None:
            return
        if now - self._last_prune_at < 60.0:
            return
        self._last_prune_at = now
        self.metrics.snapshots_pruned += self.storage.prune_reward_snapshots(
            self.run_id, now - retention
        )

    def _handle_pair(
        self,
        pair: RewardPair,
        size_shares: float,
        *,
        force_snapshot: bool = False,
    ) -> None:
        assert self.books is not None
        now = time.time()
        self._settle_pending_cancels(now)
        if pair.condition_id in self.halted_markets:
            # Retired by stop-loss: cancel any resting quotes, plan nothing, hold inventory.
            self._cancel_all_quotes_for_pair(pair, now)
            return
        self._update_market_cooldown(pair, now)
        exit_plans = self._manage_inventory(pair, now)
        if pair.condition_id in self.halted_markets:
            # Stop-loss tripped this very cycle: go dark instead of recovery-quoting.
            self._cancel_all_quotes_for_pair(pair, now)
            return
        entry_plans = (
            self.planner.plan_pair(
                pair,
                self.books,
                size_shares,
                distance_fraction=self._entry_distance_fraction(pair),
            )
            if self._can_plan_entry_or_hedge(pair, now)
            else []
        )
        plans = self._filter_entry_plans_for_inventory(pair, entry_plans) + exit_plans
        planned_keys = {plan.key() for plan in plans}
        active_keys = {
            key for key in self.active_quotes if key[0] == pair.condition_id
        }
        pulled_keys = active_keys - planned_keys
        for key in pulled_keys:
            active = self.active_quotes.pop(key)
            self._request_cancel_quote(
                active,
                reason="planner_no_quote",
                final_status="cancelled",
                now=now,
            )
            self.metrics.quote_cancellations += 1
        if pulled_keys:
            # Quotes were dropped (gap-guard/trend cooldown, inventory filter, ...): push the
            # cancels to the exchange immediately rather than on the next periodic reconcile.
            self._signal_live_reconcile()
        adverse_reprice = False
        for plan in plans:
            key = plan.key()
            active = self.active_quotes.get(key)
            if active is not None and (
                abs(active.plan.price - plan.price) > 1e-12
                or abs(active.plan.size - plan.size) > 1e-12
            ):
                held_plan = self._depth_hold_plan(pair, active, plan, now)
                if held_plan is None:
                    held_plan = self._live_buy_chase_hold_plan(pair, active, plan, now)
                if held_plan is not None:
                    active.plan = held_plan
                    self._update_quote_state(
                        pair, active, now, force_snapshot=force_snapshot
                    )
                    continue
                if self._is_adverse_reprice(active.plan, plan):
                    adverse_reprice = True
                self.active_quotes.pop(key, None)
                self._request_cancel_quote(
                    active,
                    reason="quote_price_changed",
                    final_status="replaced",
                    now=now,
                )
                self.metrics.quote_replacements += 1
                active = None
            if active is None:
                active = self._open_quote(pair, plan, now)
                self.active_quotes[key] = active
            else:
                active.plan = plan
            self._update_quote_state(pair, active, now, force_snapshot=force_snapshot)
        if adverse_reprice:
            # The market is moving toward a resting entry: push the new (safer) price now rather
            # than on the periodic tick. The reconcile floor (min_reconcile_seconds) bounds churn.
            self._signal_live_reconcile()
        self._account_market_scoring(pair, now)

    @staticmethod
    def _is_adverse_reprice(old: RewardQuotePlan, new: RewardQuotePlan) -> bool:
        """True when a reprice moves a resting BUY entry toward the market (higher fill risk).

        Only BUY entries can open an unwanted position on an adverse fill, so only they are
        rushed. Lowering a resting bid means the market is falling toward it: the old quote is
        now too high for the new midpoint, so the cancel/repost should not wait for a periodic
        reconcile. A BUY moving up is a reward-score chase, not an immediate safety action; live
        mode separately suppresses that chase while the old quote is still scoring.
        """
        return new.side == "BUY" and new.price < old.price - 1e-12

    def _depth_hold_plan(
        self,
        pair: RewardPair,
        active: ActiveRewardQuote,
        new: RewardQuotePlan,
        now: float,
    ) -> RewardQuotePlan | None:
        """Keep a depth-placed BUY at its current price while it stays inside the hold band.

        The two band edges are NOT symmetric, which is what stops the quote oscillating between
        two adjacent ticks when a single large book level straddles the band:

        * ``floor`` is a hard SAFETY bound — depth ahead below it means a taker could reach us,
          so we always release the hold and let the reprice move us deeper.
        * ``ceil`` is only a REWARD preference (we would rather not sit needlessly deep). We only
          release it to move UP toward mid when the shallower target the planner picked is itself
          ROBUSTLY protected (depth there ``>= target``). If the shallower tick is only marginally
          protected (``[floor, target)``) or below floor, moving up would just bounce us back next
          tick, so we HOLD the deeper tick and accept a little lost reward for a stable quote.

        A risk-driven shrink and a fall out of the reward zone both still release the hold.
        """
        if not self.settings.reward_mm_depth_based_placement or self.books is None:
            return None
        if new.side != "BUY":
            return None
        old = active.plan
        if old.side != "BUY" or old.size <= 0:
            return None
        if new.size + 1e-9 < old.size:
            return None
        book = self.books.get(old.token_id)
        if book is None:
            return None
        min_shares = float(pair.rewards_min_size)
        floor_mult, target_mult, ceil_mult = depth_mults_for_min_shares(min_shares)
        floor_buffer = floor_mult * min_shares
        target_buffer = target_mult * min_shares
        ceil_buffer = ceil_mult * min_shares
        tick = max(float(new.tick_size or old.tick_size or 0.0), 1e-9)
        depth = float(book.cumulative_depth_ahead("BUY", old.price, own_size=old.size))
        if depth + 1e-9 < floor_buffer:
            # Too thin ahead: safety release — let the reprice move us deeper.
            return None
        if depth > ceil_buffer + 1e-9:
            # Over-protected: only release the upward (toward-mid) move when the planner's
            # shallower target is robustly in-band; otherwise hold to avoid tick oscillation.
            moving_up = new.price > old.price + tick / 2
            if moving_up:
                depth_new = float(book.cumulative_depth_ahead("BUY", new.price))
                if depth_new + 1e-9 >= target_buffer:
                    return None
        refreshed = self._refresh_plan_against_book(pair, old)
        if refreshed is None or not refreshed.locally_scoring:
            # Fell out of the reward zone / not post-only safe — let mid-tracking reprice.
            return None
        return refreshed

    def _live_buy_chase_hold_plan(
        self,
        pair: RewardPair,
        active: ActiveRewardQuote,
        new: RewardQuotePlan,
        now: float,
    ) -> RewardQuotePlan | None:
        """Keep a live BUY at its old price when raising it is only score chasing.

        The reward planner still targets the configured score point (not the far edge). This
        guard only blocks immediate upward live replacements when the existing BUY remains
        reward-eligible under the current book. If the old quote falls out of the reward zone,
        needs to shrink for risk, is part of inventory completion, or has aged long enough, the
        normal reprice goes through.
        """
        if self.live is None or new.side != "BUY":
            return None
        old = active.plan
        tick = max(float(new.tick_size or old.tick_size or 0.0), 1e-9)
        if new.price <= old.price + tick / 2:
            return None
        if old.size > new.size + 1e-12:
            return None
        if self._open_inventory_for_pair(pair.condition_id):
            return None
        delay = max(0.0, self.settings.reward_mm_live_buy_chase_delay_seconds)
        if delay <= 0:
            return None
        delay = max(delay, float(pair.minimum_order_age_seconds or 0))
        if now - active.opened_at >= delay:
            return None
        refreshed = self._refresh_plan_against_book(pair, old)
        if refreshed is None or not refreshed.locally_scoring:
            return None
        return refreshed

    def _refresh_plan_against_book(
        self, pair: RewardPair, plan: RewardQuotePlan
    ) -> RewardQuotePlan | None:
        """Recompute scoring fields for an existing quote price on the latest book."""
        if self.books is None:
            return None
        book = self.books.get(plan.token_id)
        max_spread = pair.rewards_max_spread
        if book is None or max_spread is None or max_spread <= 0:
            return None
        bid = book.best_bid()
        ask = book.best_ask()
        midpoint = book.midpoint()
        spread = book.spread()
        if bid is None or ask is None or midpoint is None or spread is None:
            return None
        tick = float(max(pair.tick_size, book.tick_size, Decimal("0.0001")))
        midpoint_f = float(midpoint)
        best_bid = float(bid)
        best_ask = float(ask)
        spread_f = float(spread)
        if best_bid <= 0 or best_ask >= 1 or best_bid >= best_ask or tick <= 0:
            return None
        if plan.side == "BUY":
            distance = max(midpoint_f - plan.price, 0.0)
            post_only_safe = plan.price <= best_ask - tick + 1e-12
        else:
            distance = max(plan.price - midpoint_f, 0.0)
            post_only_safe = plan.price >= best_bid + tick - 1e-12
        within_spread = distance <= max_spread + 1e-12
        min_size = max(pair.rewards_min_size, pair.min_order_size)
        size_eligible = plan.size + 1e-12 >= min_size
        locally_scoring = within_spread and post_only_safe and size_eligible
        score_weight = (
            self.planner._distance_score(max_spread, distance) * plan.size
            if locally_scoring
            else 0.0
        )
        reason = ""
        if not size_eligible:
            reason = "below_min_size"
        elif not within_spread:
            reason = "outside_reward_spread"
        elif not post_only_safe:
            reason = "not_post_only_safe"
        return replace(
            plan,
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

    def _signal_live_reconcile(self) -> None:
        """Ask the live coordinator (if any) to reconcile now instead of on its next tick.

        Called when a quote is pulled for an adverse reason or a fill is booked, so the stale
        live order leaves the book in ~ms rather than waiting up to a full reconcile interval.
        These are safety actions, so the reconcile is requested as ``urgent`` to bypass the live
        loop's inter-reconcile floor. No-op in paper mode. Never lets a live error escape into
        the engine loop.
        """
        live = self.live
        if live is None:
            return
        with contextlib.suppress(Exception):
            live.request_reconcile(urgent=True)  # type: ignore[attr-defined]

    def _request_cancel_quote(
        self,
        active: ActiveRewardQuote,
        *,
        reason: str,
        final_status: str,
        now: float,
    ) -> None:
        if active.lifecycle_state == "cancel_requested":
            return
        latency_seconds = max(0.0, self.settings.reward_mm_cancel_latency_ms) / 1000.0
        active.lifecycle_state = "cancel_requested"
        active.cancel_requested_at = now
        active.cancel_ack_at = now + latency_seconds
        active.cancel_reason = reason
        active.cancel_final_status = final_status
        self.pending_cancel_quotes[active.quote_uid] = active
        pair = self.pair_cache.get(active.plan.condition_id) or self.pairs.get(
            active.plan.condition_id
        )
        if pair is not None:
            self.storage.insert_reward_quote(self._quote_record(active, pair, now))
        if latency_seconds <= 1e-12:
            self._settle_pending_cancels(now)

    def _settle_pending_cancels(self, now: float) -> None:
        acked: list[str] = []
        for quote_uid, active in list(self.pending_cancel_quotes.items()):
            if active.cancel_ack_at is None or active.cancel_ack_at > now + 1e-12:
                continue
            closed_at = active.cancel_ack_at
            self.storage.close_reward_quote(
                quote_uid,
                status=active.cancel_final_status,
                reason=active.cancel_reason,
                closed_at=closed_at,
            )
            self.metrics.cancel_acknowledged_quotes += 1
            if active.cancel_requested_at is not None:
                self.metrics.cancel_latency_seconds_total += max(
                    0.0, closed_at - active.cancel_requested_at
                )
            acked.append(quote_uid)
        for quote_uid in acked:
            self.pending_cancel_quotes.pop(quote_uid, None)

    def _live_quotes(self) -> list[ActiveRewardQuote]:
        return [*self.active_quotes.values(), *self.pending_cancel_quotes.values()]

    def _filter_entry_plans_for_inventory(
        self, pair: RewardPair, plans: list[RewardQuotePlan]
    ) -> list[RewardQuotePlan]:
        if self._open_inventory_for_pair(pair.condition_id):
            # Holding inventory: the post-fill race (passive sell + arb-capped completion buy)
            # owns both legs of this market from _manage_inventory. Pause fresh entry quotes here
            # so we never add to or duplicate the managed position; the completion buy is emitted
            # by _passive_completion_plan and is deliberately NOT subject to the entry exposure
            # cap (a hedge of an existing position, not a new entry).
            return []
        if self.settings.reward_mm_max_net_delta_shares > 0:
            unhedged = self._inventory_risk_summary()["unhedged_shares"]
            if unhedged >= self.settings.reward_mm_max_net_delta_shares:
                return []
        filtered: list[RewardQuotePlan] = []
        # Per-market exposure: only THIS market's own open inventory counts against its entry
        # budget, so a fill on another market never collapses two-sided quoting here. A market
        # that holds inventory already returned [] above, so this is 0 for anything still quoting;
        # it then only accumulates this market's own planned legs below.
        planned_exposure = self._current_inventory_exposure(pair.condition_id)
        for plan in plans:
            allowed_size = self._apply_new_entry_risk_limits(
                plan, plan.size, planned_exposure
            )
            if allowed_size + 1e-12 < max(pair.rewards_min_size, pair.min_order_size):
                continue
            resized = self._resize_plan(pair, plan, allowed_size)
            if resized is not None:
                filtered.append(resized)
                planned_exposure += resized.size * resized.price
        return filtered

    def _apply_new_entry_risk_limits(
        self, plan: RewardQuotePlan, size: float, current_exposure: float
    ) -> float:
        allowed_size = self._entry_size_cap(plan, size)
        if self.max_position_shares > 0:
            allowed_size = min(allowed_size, self.max_position_shares)
        if self.max_exposure_usdc > 0:
            available_usdc = max(0.0, self.max_exposure_usdc - current_exposure)
            if available_usdc <= 1e-12:
                return 0.0
            allowed_size = min(allowed_size, available_usdc / max(plan.price, 1e-9))
        return allowed_size

    def _entry_size_cap(self, plan: RewardQuotePlan, base_size: float) -> float:
        """Cap quote size to visible top-of-book depth, never to one-day volatility."""
        cap = base_size
        book = self.books.get(plan.token_id) if self.books else None
        if book is not None:
            depth = float(book.best_bid_depth()) + float(book.best_ask_depth())
            if depth > 0:
                multiplier = max(1.0, self.settings.reward_mm_entry_depth_multiplier)
                cap = min(cap, depth / multiplier)
        return cap

    def _resize_plan(
        self, pair: RewardPair, plan: RewardQuotePlan, size: float
    ) -> RewardQuotePlan | None:
        if size <= 1e-12:
            return None
        if abs(size - plan.size) <= 1e-12:
            return plan
        min_size = max(pair.rewards_min_size, pair.min_order_size)
        size_eligible = size + 1e-12 >= min_size
        locally_scoring = plan.within_spread and plan.post_only_safe and size_eligible
        score_weight = (
            self.planner._distance_score(plan.max_spread, plan.distance_from_midpoint) * size
            if locally_scoring
            else 0.0
        )
        reason = ""
        if not size_eligible:
            reason = "below_min_size"
        elif not plan.within_spread:
            reason = "outside_reward_spread"
        elif not plan.post_only_safe:
            reason = "not_post_only_safe"
        return replace(
            plan,
            size=size,
            size_eligible=size_eligible,
            locally_scoring=locally_scoring,
            score_weight=score_weight,
            reason=reason,
        )

    def _manage_inventory(self, pair: RewardPair, now: float) -> list[RewardQuotePlan]:
        assert self.books is not None
        exit_plans: list[RewardQuotePlan] = []
        for item in list(self._open_inventory_for_pair(pair.condition_id)):
            self._refresh_inventory_mark(pair, item, now)
            stop = self._inventory_stop(item, self.max_loss_usdc)
            if stop is not None:
                reason, trigger_pnl, loss_cap = stop
                if self.settings.reward_mm_halt_market_on_stop_loss:
                    # Unattended-safety: retire this market for the rest of the run.
                    self._halt_market(item.condition_id, reason)
                if self.live is not None:
                    throttle = self.settings.reward_mm_stop_action_throttle_seconds
                    last = self._last_stop_action_at.get(item.condition_id, 0.0)
                    if throttle > 0 and now - last < throttle:
                        # Acted on this stop very recently (e.g. a just-filled position whose
                        # shares have not settled, so the FOK keeps failing): hold and re-check
                        # next tick instead of spamming retries/alerts every ~0.1s.
                        continue
                    self._last_stop_action_at[item.condition_id] = now
                    route = self._best_emergency_close_route(item)
                    if route is not None and self._auto_exit_eligible(item):
                        # Live small-size auto-cut: take the cheaper of (sell the held leg /
                        # complete the set) with a price-capped FOK. The FOK either fills fully at
                        # a price no worse than the slippage cap or is killed; on a kill we fall
                        # through to the manual alert so the position is never left unmanaged.
                        executed = self.live.execute_taker_exit(  # type: ignore[attr-defined]
                            pair, item, route
                        )
                        if executed:
                            self._close_inventory(
                                item,
                                close_mode="auto_taker_exit",
                                realized_pnl=route.realized_pnl,
                                now=now,
                                exit_price=route.exit_price,
                                hedge_price=route.hedge_price,
                                bundle_cost=route.bundle_cost,
                                risk_reason=reason,
                                trigger_pnl=trigger_pnl,
                                liquidation_pnl_at_trigger=item.liquidation_pnl,
                                hedge_pnl_at_trigger=item.best_hedge_pnl,
                                exit_route=route.route,
                                effective_max_loss=loss_cap,
                            )
                            continue
                    # Large position, no route, auto-exit disabled, or FOK killed: cancel this
                    # market's orders, alert the user to handle the taker exit, and pause re-entry.
                    self.live.alert_emergency(item, reason, trigger_pnl)  # type: ignore[attr-defined]
                    self._set_cooldown(
                        item.condition_id, now, reason=f"emergency:{reason}", move=0.0
                    )
                    continue
                route = self._best_emergency_close_route(item)
                if route is None:
                    continue
                self._close_inventory(
                    item,
                    close_mode="emergency_exit",
                    realized_pnl=route.realized_pnl,
                    now=now,
                    exit_price=route.exit_price,
                    hedge_price=route.hedge_price,
                    bundle_cost=route.bundle_cost,
                    risk_reason=reason,
                    trigger_pnl=trigger_pnl,
                    liquidation_pnl_at_trigger=item.liquidation_pnl,
                    hedge_pnl_at_trigger=item.best_hedge_pnl,
                    exit_route=route.route,
                    effective_max_loss=loss_cap,
                )
                continue
            hedge = self._best_taker_hedge(pair, item)
            if hedge is not None:
                hedge_price, bundle_cost, hedge_pnl = hedge
                item.best_hedge_price = hedge_price
                item.best_hedge_bundle_cost = bundle_cost
                item.best_hedge_pnl = hedge_pnl
                # Fixed, credit-independent arb-completion trigger: lock the bundle only when the
                # set cost is at/under the ceiling. Banked reward credit is deliberately NOT used
                # to justify a larger voluntary completion loss (see config note); it stays a
                # reporting figure applied in _close_inventory.
                if (
                    self.settings.reward_mm_arb_close_after_fill
                    and bundle_cost <= self.settings.reward_mm_arb_close_max_cost + 1e-12
                ):
                    if self.live is not None:
                        # Live: actually buy the opposite leg (FOK taker) to lock the bundle.
                        # If the lock fails (price moved / killed), keep the position open.
                        locked = self.live.execute_bundle_lock(  # type: ignore[attr-defined]
                            pair, item, hedge_price
                        )
                        if not locked:
                            self._store_inventory(item, now, mark=True)
                            continue
                    self._close_inventory(
                        item,
                        close_mode="hedge_locked",
                        realized_pnl=hedge_pnl,
                        now=now,
                        hedge_price=hedge_price,
                        bundle_cost=bundle_cost,
                    )
                    continue
            exit_plan = self._passive_exit_plan(pair, item)
            if exit_plan is not None:
                item.state = "passive_exit_quoted"
                item.passive_exit_price = exit_plan.price
                exit_plans.append(exit_plan)
            else:
                item.state = "open_inventory"
                item.passive_exit_price = None
                item.passive_exit_quote_uid = None
            # Other half of the race: a resting completion buy on the opposite leg, priced so the
            # set cost stays within the arb ceiling. Whichever fills first resolves the position
            # (sell -> flat via _apply_fill_to_quote; completion -> hedge_locked bundle match).
            completion_plan = (
                self._passive_completion_plan(pair, item)
                if self.settings.reward_mm_arb_close_after_fill
                else None
            )
            if completion_plan is not None:
                exit_plans.append(completion_plan)
            self._store_inventory(item, now, mark=True)
        return exit_plans

    def _entry_distance_fraction(self, pair: RewardPair) -> float:
        """Quote further from midpoint on volatile markets to cut adverse selection."""
        base = self.planner.quote_distance_fraction
        k = self.settings.reward_mm_distance_vol_widen_k
        eff = base + max(0.0, k) * abs(pair.one_day_price_change)
        return min(max(eff, 0.3), 0.95)

    def _can_plan_entry_or_hedge(self, pair: RewardPair, now: float) -> bool:
        if pair.condition_id in self.markets_retired_after_fill:
            # Took a fill this run: no new entries here. The open leg is still managed by
            # _manage_inventory (called unconditionally in _handle_pair), so the race/stop/
            # auto-exit keep running; only fresh entry quoting is paused until restart.
            return False
        if self._open_inventory_for_pair(pair.condition_id):
            return True
        if self.selected_pairs and pair.condition_id not in self.selected_pairs:
            return False
        first_seen = self.market_first_seen.get(pair.condition_id)
        if (
            first_seen is not None
            and now - first_seen < self.settings.reward_mm_warmup_seconds
        ):
            self.metrics.cooldown_entry_blocks += 1
            return False
        if self._market_in_game_window(pair, now):
            self.metrics.cooldown_entry_blocks += 1
            return False
        if self._market_on_cooldown(pair.condition_id, now):
            self.metrics.cooldown_entry_blocks += 1
            return False
        return True

    def _market_in_game_window(self, pair: RewardPair, now: float) -> bool:
        """Pause new entry quotes around a sports/esports match's start time."""
        if not self.settings.reward_mm_game_pause_enabled:
            return False
        start = pair.game_start_time
        if start is None:
            return False
        start_ts = start.timestamp()
        pre = self.settings.reward_mm_game_pause_pre_seconds
        post = self.settings.reward_mm_game_pause_post_seconds
        return start_ts - pre <= now <= start_ts + post

    def _inventory_stop(
        self, item: RewardInventory, effective_max_loss: float
    ) -> tuple[str, float, float] | None:
        """Stop when mark-to-market loss hits the per-share cap, or the same-market
        hedge would lose more than the absolute ``max_loss_usdc`` ceiling.

        The per-share cap is applied only to liquidation (mark) PnL; the hedge cost
        is structurally negative by roughly the spread right after a single-leg fill,
        so it keeps the looser absolute ceiling to avoid stopping out every entry.
        """
        candidates: list[tuple[str, float, float]] = []
        if item.liquidation_pnl is not None and item.liquidation_pnl <= -effective_max_loss:
            candidates.append(("liquidation_stop", item.liquidation_pnl, effective_max_loss))
        if item.best_hedge_pnl is not None and item.best_hedge_pnl <= -self.max_loss_usdc:
            candidates.append(("hedge_stop", item.best_hedge_pnl, self.max_loss_usdc))
        if not candidates:
            return None
        return min(candidates, key=lambda candidate: candidate[1])

    def _auto_exit_eligible(self, item: RewardInventory) -> bool:
        """True when a stopped live position is small enough to cut automatically with a taker
        FOK: shares within the cap, and (if set) notional within the cap. 0 shares-cap disables
        auto-exit entirely, so the default behaviour stays manual-alert."""
        max_shares = self.settings.reward_mm_live_auto_exit_max_shares
        if max_shares <= 0 or item.shares > max_shares + 1e-9:
            return False
        max_notional = self.settings.reward_mm_live_auto_exit_max_notional_usdc
        if max_notional > 0 and item.shares * item.avg_price > max_notional + 1e-9:
            return False
        return True

    def _best_emergency_close_route(
        self, item: RewardInventory
    ) -> EmergencyCloseRoute | None:
        choices: list[EmergencyCloseRoute] = []
        if item.liquidation_pnl is not None and item.mark_price is not None:
            choices.append(
                EmergencyCloseRoute(
                    route="sell_current",
                    realized_pnl=item.liquidation_pnl,
                    exit_price=item.mark_price,
                )
            )
        if (
            item.best_hedge_pnl is not None
            and item.best_hedge_price is not None
            and item.best_hedge_bundle_cost is not None
        ):
            choices.append(
                EmergencyCloseRoute(
                    route="buy_opposite",
                    realized_pnl=item.best_hedge_pnl,
                    hedge_price=item.best_hedge_price,
                    bundle_cost=item.best_hedge_bundle_cost,
                )
            )
        if not choices:
            return None
        return max(choices, key=lambda choice: choice.realized_pnl)

    def _best_taker_hedge(
        self, pair: RewardPair, item: RewardInventory
    ) -> tuple[float, float, float] | None:
        assert self.books is not None
        if item.avg_price <= 0:
            # Unknown/zero cost basis (e.g. a wallet position adopted with no avgPrice): the
            # bundle PnL would be fabricated (avg 0 makes any completion look like free arb), so
            # never buy the opposite leg off it. Such a position is alerted and held, not traded.
            return None
        opposite_token = pair.token_b if item.token_id == pair.token_a else pair.token_a
        book = self.books.get(opposite_token)
        if book is None:
            return None
        hedge_price_dec = book.vwap_buy(self._shares_decimal(item.shares))
        if hedge_price_dec is None:
            return None
        hedge_price = float(hedge_price_dec)
        hedge_fee = taker_fee_per_share(hedge_price, pair.fee_rate)
        bundle_cost = item.avg_price + hedge_price + hedge_fee
        hedge_pnl = (1.0 - bundle_cost) * item.shares
        return hedge_price, bundle_cost, hedge_pnl

    def _passive_exit_plan(
        self, pair: RewardPair, item: RewardInventory
    ) -> RewardQuotePlan | None:
        assert self.books is not None
        book = self.books.get(item.token_id)
        if book is None:
            return None
        leg = "a" if item.token_id == pair.token_a else "b"
        minimum_price = item.avg_price - self.settings.reward_mm_scratch_loss_per_share
        return self.planner.plan_sell(
            pair=pair,
            book=book,
            leg=leg,
            outcome=item.outcome,
            size_shares=item.shares,
            minimum_price=minimum_price,
        )

    def _passive_completion_plan(
        self, pair: RewardPair, item: RewardInventory
    ) -> RewardQuotePlan | None:
        """Rest a BUY on the opposite leg, capped so completing the set costs at most the arb
        ceiling (``avg + buy_price <= reward_mm_arb_close_max_cost``; a maker completion pays no
        taker fee). Returns None when the opposite leg is too dear to complete within the ceiling
        inside the reward zone this tick — then only the passive sell rests. Only the lone-leg
        case is completed; if both legs are already held (a balanced set), no completion is added.
        """
        assert self.books is not None
        if item.avg_price <= 0:
            # Unknown/zero cost basis: never buy the opposite leg (see _best_taker_hedge).
            return None
        if len(self._open_inventory_for_pair(item.condition_id)) != 1:
            return None
        if item.token_id == pair.token_a:
            opp_token, leg, outcome = pair.token_b, "b", pair.outcome_b
        else:
            opp_token, leg, outcome = pair.token_a, "a", pair.outcome_a
        book = self.books.get(opp_token)
        if book is None:
            return None
        max_price = self.settings.reward_mm_arb_close_max_cost - item.avg_price
        if max_price <= 0:
            return None
        return self.planner.plan_buy(
            pair=pair,
            book=book,
            leg=leg,
            outcome=outcome,
            size_shares=item.shares,
            allow_depth=False,
            max_price=max_price,
        )

    def _refresh_inventory_mark(
        self, pair: RewardPair, item: RewardInventory, now: float
    ) -> None:
        assert self.books is not None
        book = self.books.get(item.token_id)
        bid = book.best_bid() if book else None
        item.mark_price = float(bid) if bid is not None else None
        hedge = self._best_taker_hedge(pair, item)
        if hedge is not None:
            item.best_hedge_price, item.best_hedge_bundle_cost, item.best_hedge_pnl = hedge
        self._store_inventory(item, now)

    def _open_inventory_for_pair(self, condition_id: str) -> list[RewardInventory]:
        return [
            item
            for item in self.inventory.values()
            if item.condition_id == condition_id and item.shares > 0 and item.closed_at is None
        ]

    def _open_inventory_condition_ids(self) -> set[str]:
        return {
            item.condition_id
            for item in self.inventory.values()
            if item.shares > 0 and item.closed_at is None
        }

    def _quote_needed_for_inventory(
        self, active: ActiveRewardQuote, open_items: list[RewardInventory]
    ) -> bool:
        if not open_items:
            return False
        if active.plan.side == "SELL":
            return any(item.token_id == active.plan.token_id for item in open_items)
        if active.plan.side == "BUY":
            return any(item.token_id != active.plan.token_id for item in open_items)
        return False

    def _cancel_stale_quotes(self, now: float) -> None:
        selected_ids = set(self.selected_pairs)
        if not selected_ids:
            return
        to_cancel: list[tuple[tuple[str, str, str], ActiveRewardQuote]] = []
        for key, active in self.active_quotes.items():
            condition_id = active.plan.condition_id
            if condition_id in selected_ids:
                continue
            open_items = self._open_inventory_for_pair(condition_id)
            if not self._quote_needed_for_inventory(active, open_items):
                to_cancel.append((key, active))
        if not to_cancel:
            return
        for key, active in to_cancel:
            self.active_quotes.pop(key, None)
            self._request_cancel_quote(
                active,
                reason="market_unwatched",
                final_status="cancelled",
                now=now,
            )
        self.metrics.quote_cancellations += len(to_cancel)

    def _pair_midpoints(self, pair: RewardPair) -> tuple[float, float] | None:
        assert self.books is not None
        book_a = self.books.get(pair.token_a)
        book_b = self.books.get(pair.token_b)
        midpoint_a = book_a.midpoint() if book_a else None
        midpoint_b = book_b.midpoint() if book_b else None
        if midpoint_a is None or midpoint_b is None:
            return None
        return float(midpoint_a), float(midpoint_b)

    def _update_market_cooldown(self, pair: RewardPair, now: float) -> None:
        midpoints = self._pair_midpoints(pair)
        if midpoints is None:
            return
        cid = pair.condition_id
        self.market_first_seen.setdefault(cid, now)
        # Smoothed midpoint (EMA) used only to detect instability, not to price quotes.
        alpha = self.settings.reward_mm_midpoint_ema_alpha
        prev_ema = self.market_midpoint_ema.get(cid)
        if prev_ema is None or alpha <= 0:
            ema = midpoints
        else:
            ema = (
                alpha * midpoints[0] + (1.0 - alpha) * prev_ema[0],
                alpha * midpoints[1] + (1.0 - alpha) * prev_ema[1],
            )
        self.market_midpoint_ema[cid] = ema
        band = self.settings.reward_mm_stability_band
        if band > 0 and prev_ema is not None:
            deviation = max(abs(midpoints[0] - ema[0]), abs(midpoints[1] - ema[1]))
            if deviation > band:
                self._set_cooldown(cid, now, reason=f"unstable midpoint {deviation:.4f}",
                                   move=deviation)
        window = self._record_midpoint_window(cid, now, midpoints)
        self._apply_runtime_volatility_guard(cid, now, window)
        # Trend/drift detector: pause entries on directional movement over the window
        # (live match / news gap); choppy oscillation has net drift ~0 and keeps quoting.
        trend_window_seconds = self.settings.reward_mm_trend_window_seconds
        drift_limit = self.settings.reward_mm_trend_drift
        if trend_window_seconds > 0 and drift_limit > 0:
            drift = self._window_drift(cid, now=now, window_seconds=trend_window_seconds)
            if drift >= drift_limit:
                self._set_cooldown(cid, now, reason=f"trending {drift:.4f}", move=drift)
        # Existing gap-guard: pause on a fast single jump.
        move_limit = self.settings.reward_mm_gap_guard_move
        previous = self.market_midpoint_marks.get(cid)
        self.market_midpoint_marks[cid] = (now, midpoints[0], midpoints[1])
        if move_limit <= 0 or previous is None:
            return
        previous_ts, previous_a, previous_b = previous
        if now - previous_ts > self.settings.reward_mm_gap_guard_window_seconds:
            return
        move = max(abs(midpoints[0] - previous_a), abs(midpoints[1] - previous_b))
        if move >= move_limit:
            self._set_cooldown(cid, now, reason=f"midpoint move {move:.4f}", move=move)

    def _record_midpoint_window(
        self, condition_id: str, now: float, midpoints: tuple[float, float]
    ) -> list[tuple[float, float, float]]:
        window = self.market_midpoint_window.setdefault(condition_id, [])
        window.append((now, midpoints[0], midpoints[1]))
        retention = max(
            self.settings.reward_mm_trend_window_seconds
            if self.settings.reward_mm_trend_drift > 0
            else 0.0,
            self.settings.reward_mm_runtime_volatility_window_seconds
            if self.settings.reward_mm_runtime_volatility_range > 0
            else 0.0,
            self.settings.reward_mm_runtime_jump_window_seconds
            if self.settings.reward_mm_runtime_jump_move > 0
            else 0.0,
        )
        if retention > 0:
            cutoff = now - retention
            while len(window) > 1 and window[0][0] < cutoff:
                window.pop(0)
        else:
            del window[:-1]
        return window

    def _apply_runtime_volatility_guard(
        self, condition_id: str, now: float, window: list[tuple[float, float, float]]
    ) -> None:
        long_window = self.settings.reward_mm_runtime_volatility_window_seconds
        range_limit = self.settings.reward_mm_runtime_volatility_range
        if long_window > 0 and range_limit > 0:
            price_range = self._window_range(
                condition_id, now=now, window_seconds=long_window, window=window
            )
            if price_range >= range_limit:
                self._set_cooldown(
                    condition_id,
                    now,
                    reason=f"midpoint range {price_range:.4f}/{long_window:g}s",
                    move=price_range,
                )
                return

        jump_window = self.settings.reward_mm_runtime_jump_window_seconds
        jump_limit = self.settings.reward_mm_runtime_jump_move
        if jump_window > 0 and jump_limit > 0:
            jump = self._window_range(
                condition_id, now=now, window_seconds=jump_window, window=window
            )
            if jump >= jump_limit:
                self._set_cooldown(
                    condition_id,
                    now,
                    reason=f"midpoint jump {jump:.4f}/{jump_window:g}s",
                    move=jump,
                )

    def _window_slice(
        self,
        condition_id: str,
        *,
        now: float,
        window_seconds: float,
        window: list[tuple[float, float, float]] | None = None,
    ) -> list[tuple[float, float, float]]:
        source = self.market_midpoint_window.get(condition_id) if window is None else window
        if not source or window_seconds <= 0:
            return []
        cutoff = now - window_seconds
        return [point for point in source if point[0] >= cutoff]

    def _window_range(
        self,
        condition_id: str,
        *,
        now: float,
        window_seconds: float,
        window: list[tuple[float, float, float]] | None = None,
    ) -> float:
        points = self._window_slice(
            condition_id, now=now, window_seconds=window_seconds, window=window
        )
        if len(points) < 2:
            return 0.0
        values_a = [point[1] for point in points]
        values_b = [point[2] for point in points]
        return max(max(values_a) - min(values_a), max(values_b) - min(values_b))

    def _window_drift(
        self,
        condition_id: str,
        *,
        now: float | None = None,
        window_seconds: float | None = None,
    ) -> float:
        """Net directional midpoint move across the trend window (max over both legs)."""
        if now is not None and window_seconds is not None:
            window = self._window_slice(condition_id, now=now, window_seconds=window_seconds)
        else:
            window = self.market_midpoint_window.get(condition_id)
        if not window or len(window) < 2:
            return 0.0
        first, last = window[0], window[-1]
        return max(abs(last[1] - first[1]), abs(last[2] - first[2]))

    def _set_cooldown(self, condition_id: str, now: float, *, reason: str, move: float) -> None:
        previous_until = self.market_cooldowns.get(condition_id, 0.0)
        until = now + self.settings.reward_mm_gap_guard_cooldown_seconds
        self.market_cooldowns[condition_id] = max(previous_until, until)
        if self.run_id is not None and until > previous_until + 1e-12:
            pair = self.pair_cache.get(condition_id) or self.pairs.get(condition_id)
            question = pair.question if pair is not None else condition_id
            self.storage.insert_reward_event(
                self.run_id,
                "market_cooldown",
                f"{question}: entry quotes paused ({reason})",
                level="warning",
                payload={"condition_id": condition_id, "move": move, "cooldown_until": until},
            )

    def _market_on_cooldown(self, condition_id: str, now: float) -> bool:
        until = self.market_cooldowns.get(condition_id, 0.0)
        if until <= now:
            self.market_cooldowns.pop(condition_id, None)
            return False
        return True

    def _retire_market_after_fill(self, condition_id: str) -> None:
        """Block NEW entries on a market for the rest of the run after it takes a fill.

        Unlike ``_halt_market`` this does NOT cancel resting quotes or stop ``_manage_inventory``:
        the post-fill race/stop/auto-exit keeps managing the open leg. It only stops the market
        from opening fresh entry quotes — a market that just filled (likely informed/volatile) is
        not re-entered this run. In-memory only, so a restart re-enables fresh entries.
        """
        if not self.settings.reward_mm_retire_market_after_fill:
            return
        if condition_id in self.markets_retired_after_fill:
            return
        self.markets_retired_after_fill.add(condition_id)
        if self.run_id is not None:
            self.storage.insert_reward_event(
                self.run_id,
                "market_retired_after_fill",
                f"{condition_id[:12]}: new entries paused after fill — managing open leg only, "
                f"re-enables on restart",
                level="warning",
            )

    def _halt_market(self, condition_id: str, reason: str) -> None:
        """Retire a market for the rest of the run after a stop-loss (opt-in safety).

        In-memory only, so a restart re-enables it. The open position is held (live
        never auto-sells at a loss); the market simply stops quoting entirely.
        """
        if condition_id in self.halted_markets:
            return
        self.halted_markets.add(condition_id)
        self.metrics.halted_markets = len(self.halted_markets)
        if self.run_id is not None:
            self.storage.insert_reward_event(
                self.run_id,
                "market_halted",
                f"{condition_id[:12]}: stop-loss halt ({reason}) — market retired until restart",
                level="warning",
            )

    def _cancel_all_quotes_for_pair(self, pair: RewardPair, now: float) -> None:
        """Cancel every tracked quote on a market so reconcile pulls its live orders."""
        keys = [k for k in self.active_quotes if k[0] == pair.condition_id]
        for key in keys:
            active = self.active_quotes.pop(key)
            self._request_cancel_quote(
                active, reason="market_halted", final_status="cancelled", now=now
            )
            self.metrics.quote_cancellations += 1
        if keys:
            # Stop-loss/halt is time-critical: cancel on the exchange now, not on the next tick.
            self._signal_live_reconcile()

    def _current_inventory_exposure(self, condition_id: str | None = None) -> float:
        """Open-inventory notional in USDC. With ``condition_id`` set, only that market's
        inventory counts — the entry exposure cap is applied per-market, so a fill on one market
        never shrinks the quoting budget of the others."""
        return sum(
            item.avg_price * item.shares
            for item in self.inventory.values()
            if item.shares > 0
            and item.closed_at is None
            and (condition_id is None or item.condition_id == condition_id)
        )

    def _inventory_risk_summary(self) -> dict[str, float]:
        by_condition: dict[str, list[RewardInventory]] = {}
        for item in self.inventory.values():
            if item.shares > 0 and item.closed_at is None:
                by_condition.setdefault(item.condition_id, []).append(item)
        matched_positions = 0
        matched_shares_total = 0.0
        matched_pnl_total = 0.0
        unhedged_shares_total = 0.0
        unhedged_pnl_total = 0.0
        for items in by_condition.values():
            matched_shares = 0.0
            if len(items) >= 2:
                first, second = items[0], items[1]
                matched_shares = min(first.shares, second.shares)
                if matched_shares > 1e-12:
                    matched_positions += 1
                    matched_shares_total += matched_shares
                    matched_pnl_total += (1.0 - first.avg_price - second.avg_price) * matched_shares
            for item in items:
                unhedged_shares = item.shares - matched_shares if len(items) >= 2 else item.shares
                if unhedged_shares <= 1e-12:
                    continue
                unhedged_shares_total += unhedged_shares
                if item.mark_price is not None:
                    unhedged_pnl_total += (item.mark_price - item.avg_price) * unhedged_shares
        return {
            "matched_bundle_positions": float(matched_positions),
            "matched_bundle_shares": matched_shares_total,
            "matched_bundle_pnl": matched_pnl_total,
            "unhedged_shares": unhedged_shares_total,
            "unhedged_pnl": unhedged_pnl_total,
        }

    @staticmethod
    def _shares_decimal(shares: float) -> Decimal:
        return Decimal(str(shares))

    def _open_quote(
        self, pair: RewardPair, plan: RewardQuotePlan, now: float
    ) -> ActiveRewardQuote:
        assert self.run_id is not None
        self.quote_sequence += 1
        quote_uid = (
            f"reward-{self.run_id}-{plan.condition_id[:10]}-"
            f"{plan.leg}-{self.quote_sequence}"
        )
        active = ActiveRewardQuote(
            quote_uid=quote_uid,
            plan=plan,
            opened_at=now,
            queue_ahead=self._queue_ahead(plan),
            last_eval_at=now,
        )
        self.metrics.quotes_opened += 1
        if plan.side == "SELL":
            item = self.inventory.get((plan.condition_id, plan.token_id))
            if item is not None and item.closed_at is None and item.shares > 0:
                item.passive_exit_quote_uid = quote_uid
                item.passive_exit_price = plan.price
                self._store_inventory(item, now)
        self.storage.insert_reward_quote(self._quote_record(active, pair, now))
        return active

    def _update_quote_state(
        self,
        pair: RewardPair,
        active: ActiveRewardQuote,
        now: float,
        *,
        force_snapshot: bool,
    ) -> None:
        age = max(0.0, now - active.opened_at)
        active.effective_scoring = (
            active.plan.locally_scoring and age >= pair.minimum_order_age_seconds
        )
        record_sig = (
            active.plan.price,
            active.plan.size,
            active.lifecycle_state,
            active.effective_scoring,
            active.plan.reason,
        )
        if record_sig != active.last_record_sig:
            self.storage.insert_reward_quote(self._quote_record(active, pair, now))
            active.last_record_sig = record_sig
        interval = self.settings.reward_mm_snapshot_interval_seconds
        if force_snapshot or now - active.last_snapshot_at >= interval:
            self.storage.insert_reward_quote_snapshots(
                [self._quote_snapshot(active, pair, now)]
            )
            self.metrics.quote_snapshots += 1
            active.last_snapshot_at = now

    def _quote_record(
        self, active: ActiveRewardQuote, pair: RewardPair, now: float
    ) -> dict[str, object]:
        plan = active.plan
        status = "cancel_requested" if active.lifecycle_state == "cancel_requested" else "open"
        reason = active.cancel_reason if active.lifecycle_state == "cancel_requested" else (
            "" if active.effective_scoring else plan.reason or "minimum_age_pending"
        )
        return {
            "quote_uid": active.quote_uid,
            "run_id": self.run_id,
            "condition_id": plan.condition_id,
            "token_id": plan.token_id,
            "outcome": plan.outcome,
            "leg": plan.leg,
            "side": plan.side,
            "price": plan.price,
            "size": plan.size,
            "distance_from_midpoint": plan.distance_from_midpoint,
            "max_spread": plan.max_spread,
            "score_weight": plan.score_weight,
            "post_only_safe": plan.post_only_safe,
            "size_eligible": plan.size_eligible,
            "locally_scoring": active.effective_scoring,
            "lifecycle_state": active.lifecycle_state,
            "cancel_requested_at": active.cancel_requested_at,
            "cancel_ack_at": active.cancel_ack_at,
            "cancel_reason": active.cancel_reason,
            "status": status,
            "reason": reason,
            "opened_at": active.opened_at,
            "updated_at": now,
        }

    def _quote_snapshot(
        self, active: ActiveRewardQuote, pair: RewardPair, now: float
    ) -> dict[str, object]:
        plan = active.plan
        # Actual resting book depth "ahead" of our order right now (own size netted out) — the
        # buffer a taker must eat to reach us. Logged so a fill can be diagnosed after the fact:
        # was the protective depth really there and it collapsed, or was it never there.
        depth_ahead: float | None = None
        if self.books is not None:
            book = self.books.get(plan.token_id)
            if book is not None:
                depth_ahead = float(
                    book.cumulative_depth_ahead(plan.side, plan.price, own_size=plan.size)
                )
        coverage = 1.0 if active.effective_scoring else 0.0
        estimated_reward = (
            pair.rewards_rate_per_day
            * self.settings.reward_mm_assumed_reward_share
            * coverage
            / 86_400
        )
        return {
            "run_id": self.run_id,
            "quote_uid": active.quote_uid,
            "condition_id": plan.condition_id,
            "token_id": plan.token_id,
            "outcome": plan.outcome,
            "side": plan.side,
            "price": plan.price,
            "size": plan.size,
            "midpoint": plan.midpoint,
            "best_bid": plan.best_bid,
            "best_ask": plan.best_ask,
            "spread": plan.spread,
            "distance_from_midpoint": plan.distance_from_midpoint,
            "max_spread": plan.max_spread,
            "depth_ahead": depth_ahead,
            "age_seconds": max(0.0, now - active.opened_at),
            "post_only_safe": plan.post_only_safe,
            "size_eligible": plan.size_eligible,
            "within_spread": plan.within_spread,
            "locally_scoring": active.effective_scoring,
            "score_weight": plan.score_weight,
            "lifecycle_state": active.lifecycle_state,
            "estimated_reward_usdc": estimated_reward,
            "local_ts": now,
        }

    def _account_market_scoring(self, pair: RewardPair, now: float) -> None:
        previous = self.market_last_eval_at.get(pair.condition_id)
        self.market_last_eval_at[pair.condition_id] = now
        if previous is None:
            return
        elapsed = max(0.0, now - previous)
        if elapsed <= 0:
            return
        scoring = [
            active
            for active in self.active_quotes.values()
            if active.plan.condition_id == pair.condition_id and active.effective_scoring
        ]
        self.metrics.scoring_quote_seconds += elapsed * len(scoring)
        self.metrics.score_weight += elapsed * sum(active.plan.score_weight for active in scoring)
        if not scoring:
            return
        self.market_competitor_score[pair.condition_id] = self._book_competitor_q(pair)
        q_one, q_two, q_min = self._liquidity_reward_scores(pair, scoring, now)
        self.metrics.liquidity_q_one_seconds += q_one * elapsed
        self.metrics.liquidity_q_two_seconds += q_two * elapsed
        self.metrics.liquidity_q_min_seconds += q_min * elapsed
        estimates = self._liquidity_reward_estimates(pair, q_min, elapsed)
        self.metrics.liquidity_reward_conservative_usdc += estimates.conservative
        self.metrics.liquidity_reward_base_usdc += estimates.base
        self.metrics.liquidity_reward_optimistic_usdc += estimates.optimistic
        self.metrics.estimated_reward_usdc = self.metrics.liquidity_reward_base_usdc
        reward_increment = estimates.base
        open_items = self._open_inventory_for_pair(pair.condition_id)
        if open_items:
            share = reward_increment / len(open_items)
            for item in open_items:
                item.reward_credit_usdc += share
        else:
            self.market_reward_credit_available[pair.condition_id] = (
                self.market_reward_credit_available.get(pair.condition_id, 0.0)
                + reward_increment
            )

    def _liquidity_reward_scores(
        self, pair: RewardPair, scoring: list[ActiveRewardQuote], now: float
    ) -> tuple[float, float, float]:
        q_one = 0.0
        q_two = 0.0
        for active in scoring:
            plan = active.plan
            weight = plan.score_weight * self._scoring_coverage(active, now)
            if plan.token_id == pair.token_a:
                if plan.side == "BUY":
                    q_one += weight
                elif plan.side == "SELL":
                    q_two += weight
            elif plan.token_id == pair.token_b:
                if plan.side == "BUY":
                    q_two += weight
                elif plan.side == "SELL":
                    q_one += weight
        midpoint = self._score_midpoint(pair)
        if midpoint is not None and 0.10 <= midpoint <= 0.90:
            factor = max(self.settings.reward_mm_single_sided_score_factor, 1e-9)
            q_min = max(min(q_one, q_two), max(q_one, q_two) / factor)
        else:
            q_min = min(q_one, q_two)
        return q_one, q_two, q_min

    def _dust_cutoff(self, pair: RewardPair) -> float:
        """Size below which book levels are dust (excluded from midpoint/competitor)."""
        cutoff = self.settings.reward_mm_dust_size_cutoff
        return cutoff if cutoff > 0 else pair.rewards_min_size

    def _score_midpoint(self, pair: RewardPair) -> float | None:
        if self.books is None:
            return None
        book = self.books.get(pair.token_a)
        midpoint = book.adjusted_midpoint(self._dust_cutoff(pair)) if book else None
        return float(midpoint) if midpoint is not None else None

    def _liquidity_reward_estimates(
        self, pair: RewardPair, q_min: float, elapsed: float
    ) -> RewardScenarioEstimate:
        if self.settings.reward_mm_competitor_from_book:
            measured = self.market_competitor_score.get(pair.condition_id, 0.0)
            competitor_conservative = (
                measured * self.settings.reward_mm_competitor_haircut_conservative
            )
            competitor_base = measured * self.settings.reward_mm_competitor_haircut_base
            competitor_optimistic = (
                measured * self.settings.reward_mm_competitor_haircut_optimistic
            )
        else:
            competitor_conservative = (
                self.settings.reward_mm_liquidity_competitor_score_conservative
            )
            competitor_base = self.settings.reward_mm_liquidity_competitor_score_base
            competitor_optimistic = (
                self.settings.reward_mm_liquidity_competitor_score_optimistic
            )
        return RewardScenarioEstimate(
            conservative=self._liquidity_reward_increment(
                pair.rewards_rate_per_day,
                q_min,
                elapsed,
                competitor_conservative,
                self.settings.reward_mm_liquidity_share_cap_conservative,
            ),
            base=self._liquidity_reward_increment(
                pair.rewards_rate_per_day,
                q_min,
                elapsed,
                competitor_base,
                self.settings.reward_mm_liquidity_share_cap_base,
            ),
            optimistic=self._liquidity_reward_increment(
                pair.rewards_rate_per_day,
                q_min,
                elapsed,
                competitor_optimistic,
                self.settings.reward_mm_liquidity_share_cap_optimistic,
            ),
        )

    def _book_competitor_q(
        self, pair: RewardPair, books: BookStore | None = None
    ) -> float:
        """Proxy total competitor reward score from resting size inside the reward spread."""
        books = books or self.books
        if books is None:
            return 0.0
        max_spread = pair.rewards_max_spread
        if max_spread is None or max_spread <= 0:
            return 0.0
        cutoff = self._dust_cutoff(pair)
        total = 0.0
        for token in (pair.token_a, pair.token_b):
            book = books.get(token)
            if book is None:
                continue
            midpoint = book.adjusted_midpoint(cutoff)
            if midpoint is None:
                continue
            mid = float(midpoint)
            for price, size in (*book.bids.items(), *book.asks.items()):
                size_f = float(size)
                if size_f < cutoff:
                    continue
                distance = abs(float(price) - mid)
                if distance > max_spread:
                    continue
                total += self.planner._distance_score(max_spread, distance) * size_f
        return total

    def _scoring_coverage(self, active: ActiveRewardQuote, now: float) -> float:
        """Discount reward for freshly (re)placed quotes that miss minute snapshots."""
        interval = self.settings.reward_mm_snapshot_interval_seconds
        if interval <= 0:
            return 1.0
        age = max(0.0, now - active.opened_at)
        return min(1.0, age / interval)

    @staticmethod
    def _liquidity_reward_increment(
        reward_rate_per_day: float,
        q_min: float,
        elapsed: float,
        competitor_score: float,
        share_cap: float,
    ) -> float:
        if reward_rate_per_day <= 0 or q_min <= 0 or elapsed <= 0 or share_cap <= 0:
            return 0.0
        competitor = max(0.0, competitor_score)
        if competitor <= 1e-12:
            share = min(1.0, share_cap)
        else:
            share = min(share_cap, q_min / (q_min + competitor))
        return reward_rate_per_day * share * elapsed / 86_400

    def _apply_trade(
        self, trade: TradePrint, now: float, *, size_shares: float | None = None
    ) -> None:
        self._settle_pending_cancels(now)
        fillable: list[tuple[tuple[str, str, str] | str, ActiveRewardQuote, bool]] = [
            (key, active, False) for key, active in self.active_quotes.items()
        ]
        fillable.extend(
            (quote_uid, active, True)
            for quote_uid, active in self.pending_cancel_quotes.items()
            if active.cancel_ack_at is None or active.cancel_ack_at > now + 1e-12
        )
        for key, active, is_pending_cancel in list(fillable):
            plan = active.plan
            if trade.token_id != plan.token_id:
                continue
            trade_price = float(trade.price)
            side = (trade.side or "").upper()
            if plan.side == "BUY" and side and side != "SELL":
                continue
            if plan.side == "SELL" and side and side != "BUY":
                continue
            if plan.side == "BUY" and trade_price > plan.price + 1e-12:
                continue
            if plan.side == "SELL" and trade_price < plan.price - 1e-12:
                continue
            available = float(trade.size)
            if active.queue_ahead > 0:
                consumed = min(active.queue_ahead, available)
                active.queue_ahead -= consumed
                available -= consumed
            fill_size = min(plan.size, available)
            if fill_size <= 1e-12:
                continue
            self._apply_fill_to_quote(
                key,
                active,
                fill_size,
                trade_price,
                now,
                is_pending_cancel=is_pending_cancel,
                exchange_ts_ms=trade.timestamp_ms,
                size_shares=size_shares,
            )
            return

    def _apply_fill_to_quote(
        self,
        key: object,
        active: ActiveRewardQuote,
        fill_size: float,
        fill_price: float,
        now: float,
        *,
        is_pending_cancel: bool = False,
        exchange_ts_ms: int | None = None,
        size_shares: float | None = None,
        fill_reason_base: str = "trade_print_crossed",
    ) -> None:
        """Record a fill (paper or live) against ``active`` and update inventory."""
        plan = active.plan
        # A maker order can never fill more than its own size. The live user channel can report
        # a trade's TOTAL size (e.g. a taker that swept several makers, including our small
        # resting order), so clamp to the order size. Without this, a 40-share quote hit by a
        # 500-share taker recorded a phantom 500-share position — the run-19 incident that
        # then failed to hedge/exit and booked a large fictitious loss. (The paper path already
        # clamps to plan.size; this makes the live path equally safe.)
        fill_size = min(float(fill_size), float(plan.size))
        if fill_size <= 1e-12:
            return
        self.metrics.would_have_fills += 1
        fill_reason = fill_reason_base
        quote_status = "would_fill"
        lifecycle_state = active.lifecycle_state
        if is_pending_cancel:
            self.metrics.fill_before_cancel += 1
            fill_reason = f"{fill_reason_base}_before_cancel_ack"
            quote_status = "fill_before_cancel"
            lifecycle_state = "fill_before_cancel"
        rebate = self._maker_rebate_estimates(plan.condition_id, fill_size, plan.price)
        self.metrics.maker_fee_equivalent_usdc += rebate.base_fee_equivalent
        self.metrics.maker_rebate_conservative_usdc += rebate.conservative
        self.metrics.maker_rebate_base_usdc += rebate.base
        self.metrics.maker_rebate_optimistic_usdc += rebate.optimistic
        self.storage.close_reward_quote(
            active.quote_uid, status=quote_status, reason=fill_reason, closed_at=now
        )
        self.storage.insert_reward_fill(
            {
                "run_id": self.run_id,
                "quote_uid": active.quote_uid,
                "condition_id": plan.condition_id,
                "token_id": plan.token_id,
                "outcome": plan.outcome,
                "side": plan.side,
                "trade_price": fill_price,
                "quote_price": plan.price,
                "size": fill_size,
                "lifecycle_state": lifecycle_state,
                "fill_reason": fill_reason,
                "maker_fee_equivalent_usdc": rebate.base_fee_equivalent,
                "maker_rebate_conservative_usdc": rebate.conservative,
                "maker_rebate_base_usdc": rebate.base,
                "maker_rebate_optimistic_usdc": rebate.optimistic,
                "exchange_ts_ms": exchange_ts_ms,
                "local_ts": now,
            }
        )
        if plan.side == "SELL":
            item = self.inventory.get((plan.condition_id, plan.token_id))
            if item is not None and item.shares > 0:
                close_size = min(fill_size, item.shares)
                realized = (plan.price - item.avg_price) * close_size
                self._close_inventory(
                    item,
                    close_mode="passive_exit",
                    realized_pnl=realized,
                    now=now,
                    exit_price=plan.price,
                    exclude_quote_uid=active.quote_uid,
                    shares_closed=close_size,
                )
        else:
            opposite = self._opposite_open_inventory(plan)
            if opposite is not None:
                close_size = min(fill_size, opposite.shares)
                bundle_cost = opposite.avg_price + plan.price
                hedge_pnl = (1.0 - bundle_cost) * close_size
                if close_size > 1e-12:
                    self._close_inventory(
                        opposite,
                        close_mode="hedge_locked",
                        realized_pnl=hedge_pnl,
                        now=now,
                        hedge_price=plan.price,
                        bundle_cost=bundle_cost,
                        exclude_quote_uid=active.quote_uid,
                        shares_closed=close_size,
                    )
                residual_size = fill_size - close_size
                if residual_size > 1e-12:
                    self._add_inventory(plan, residual_size, plan.price, now, active.quote_uid)
            else:
                self._add_inventory(plan, fill_size, plan.price, now, active.quote_uid)
        self.storage.insert_reward_event(
            self.run_id,
            "fill_before_cancel" if is_pending_cancel else "would_have_fill",
            f"{plan.outcome} {plan.side} {fill_size:g} @ {plan.price:.4f}",
            level="warning",
        )
        if is_pending_cancel:
            self.pending_cancel_quotes.pop(str(key), None)
        else:
            self.active_quotes.pop(key, None)
        pair = self.pairs.get(plan.condition_id)
        if pair is not None and size_shares is not None:
            self._handle_pair(pair, size_shares, force_snapshot=True)
        # A fill changes the desired set (entry removed, passive exit / hedge completion added):
        # push it to the exchange immediately instead of on the next periodic reconcile.
        self._signal_live_reconcile()

    def apply_live_fill(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        *,
        now: float | None = None,
        size_shares: float | None = None,
        pending_only: bool = False,
    ) -> bool:
        """Apply a real user-channel fill to the matching resting quote (live mode).

        ``pending_only`` restricts matching to pending-cancel quotes. It is used to recover a
        fill on an order that was dropped from live tracking during its cancel race: only the
        pending-cancel quote is the actual filled order (its price equals the fill price), so
        matching an unrelated still-resting active quote — which would book a wrong cost basis
        and close a live order — is deliberately skipped.
        """
        now = now if now is not None else time.time()
        side = side.upper()
        if not pending_only:
            for key, active in list(self.active_quotes.items()):
                if active.plan.token_id == token_id and active.plan.side == side:
                    self._apply_fill_to_quote(
                        key,
                        active,
                        float(size),
                        float(price),
                        now,
                        size_shares=size_shares,
                        fill_reason_base="live_fill",
                    )
                    return True
        # An order can still fill while its cancel is in flight (the exchange hasn't acked it
        # yet). Those quotes have moved to pending_cancel_quotes, so match them too — otherwise
        # the real fill is dropped and the wallet holds an untracked position the bot won't
        # hedge or exit (the run-19 Shanghai untracked No leg, where the initial loss began).
        for quote_uid, active in list(self.pending_cancel_quotes.items()):
            if active.plan.token_id == token_id and active.plan.side == side:
                self._apply_fill_to_quote(
                    quote_uid,
                    active,
                    float(size),
                    float(price),
                    now,
                    is_pending_cancel=True,
                    size_shares=size_shares,
                    fill_reason_base="live_fill",
                )
                return True
        if self.run_id is not None:
            self.storage.insert_reward_event(
                self.run_id,
                "live_fill_unmatched",
                f"{side} {size:g} @ {price:.4f} token {token_id[:10]}",
                level="warning",
            )
        return False

    def _maker_rebate_estimates(
        self, condition_id: str, fill_size: float, price: float
    ) -> RewardScenarioEstimate:
        pair = (
            self.pair_cache.get(condition_id)
            or self.pairs.get(condition_id)
            or self.selected_pairs.get(condition_id)
        )
        fee_equivalent = 0.0
        if pair is not None and pair.fees_enabled and pair.fee_rate > 0:
            fee_equivalent = fill_size * taker_fee_per_share(price, pair.fee_rate)
        return RewardScenarioEstimate(
            conservative=fee_equivalent
            * self.settings.reward_mm_maker_rebate_rate_conservative,
            base=fee_equivalent * self.settings.reward_mm_maker_rebate_rate_base,
            optimistic=fee_equivalent
            * self.settings.reward_mm_maker_rebate_rate_optimistic,
            base_fee_equivalent=fee_equivalent,
        )

    def _add_inventory(
        self,
        plan: RewardQuotePlan,
        shares: float,
        price: float,
        now: float,
        quote_uid: str | None,
    ) -> None:
        key = (plan.condition_id, plan.token_id)
        item = self.inventory.get(key)
        if item is None or item.closed_at is not None or item.shares <= 1e-12:
            self.inventory_sequence += 1
            reward_credit = self.market_reward_credit_available.get(plan.condition_id, 0.0)
            self.market_reward_credit_available[plan.condition_id] = 0.0
            item = RewardInventory(
                condition_id=plan.condition_id,
                token_id=plan.token_id,
                outcome=plan.outcome,
                inventory_uid=(
                    f"inv-{self.run_id}-{plan.condition_id[:10]}-"
                    f"{plan.leg}-{self.inventory_sequence}"
                ),
                leg=plan.leg,
                opened_at=now,
                reward_credit_usdc=reward_credit,
            )
            self.inventory[key] = item
        item.total_cost += shares * price
        item.shares += shares
        # Took a fill here: stop opening fresh entries on this market for the rest of the run
        # (the open leg is still managed). No-op when the feature is disabled or already retired.
        self._retire_market_after_fill(plan.condition_id)
        self._store_inventory(item, now, mark=True)

    def _adopt_inventory(
        self,
        pair: RewardPair,
        leg: str,
        outcome: str,
        token_id: str,
        shares: float,
        price: float,
        now: float,
    ) -> None:
        """Create/grow inventory to absorb a wallet position the bot under-tracked.

        Like ``_add_inventory`` but plan-free (the source is the real wallet, not a quote of
        ours), so it has no reward credit to claim.
        """
        key = (pair.condition_id, token_id)
        item = self.inventory.get(key)
        if item is None or item.closed_at is not None or item.shares <= 1e-12:
            self.inventory_sequence += 1
            item = RewardInventory(
                condition_id=pair.condition_id,
                token_id=token_id,
                outcome=outcome,
                inventory_uid=(
                    f"inv-{self.run_id}-{pair.condition_id[:10]}-{leg}-{self.inventory_sequence}"
                ),
                leg=leg,
                opened_at=now,
            )
            self.inventory[key] = item
        item.total_cost += shares * price
        item.shares += shares
        self._store_inventory(item, now, mark=True)

    def reconcile_wallet_positions(self, positions: list[object], now: float | None = None) -> int:
        """Adopt real wallet positions the bot under-tracks on markets it is trading.

        Adopt-only: never auto-reduce, because the data-api can lag a just-recorded fill and a
        spurious reduce would close a correctly-tracked position. Off-universe holdings (markets
        not in the watch list) are alerted once, not adopted — the bot has no pair to manage them.
        Returns the number of positions adopted. ``positions`` items are duck-typed
        (``token_id``, ``size``, ``avg_price``, ``cur_price``, ``outcome``, ``redeemable``).
        """
        now = now if now is not None else time.time()
        min_shares = self.settings.reward_mm_position_sync_min_shares
        tracked: dict[str, tuple[RewardPair, str, str]] = {}
        for pair in self.pairs.values():
            tracked[pair.token_a] = (pair, "a", pair.outcome_a)
            tracked[pair.token_b] = (pair, "b", pair.outcome_b)
        # Net opposite legs before adopting. When the wallet holds BOTH outcome tokens on a market
        # (after execute_bundle_lock bought the hedge leg, or after a restart), the matched amount
        # min(yes, no) is a flat bundle held to redemption — no directional risk. Excluding it from
        # adoption stops the bot re-hedging an already-locked bundle (the run-48 double hedge: No-50
        # filled, Yes-50 bought to lock, then both legs re-adopted and Yes bought again). Only the
        # unmatched excess on a leg is a real position to adopt. This runs only inside reconcile (a
        # slow cadence), so it adds nothing to the quote-reposting path.
        wallet_by_token: dict[str, float] = {}
        for pos in positions:
            token = str(getattr(pos, "token_id", ""))
            if token in tracked:
                wallet_by_token[token] = wallet_by_token.get(token, 0.0) + float(
                    getattr(pos, "size", 0.0)
                )
        flat_by_token: dict[str, float] = {}
        for pair in self.pairs.values():
            flat = min(
                wallet_by_token.get(pair.token_a, 0.0), wallet_by_token.get(pair.token_b, 0.0)
            )
            if flat > 0.0:
                flat_by_token[pair.token_a] = flat
                flat_by_token[pair.token_b] = flat
        adopted = 0
        for pos in positions:
            token_id = str(getattr(pos, "token_id", ""))
            size = float(getattr(pos, "size", 0.0))
            info = tracked.get(token_id)
            if info is None:
                cur = float(getattr(pos, "cur_price", 0.0))
                is_live = (
                    size >= min_shares
                    and 0.02 <= cur <= 0.98
                    and not bool(getattr(pos, "redeemable", False))
                )
                if is_live and token_id not in self._alerted_untracked:
                    self._alerted_untracked.add(token_id)
                    if self.run_id is not None:
                        self.storage.insert_reward_event(
                            self.run_id,
                            "untracked_position",
                            f"wallet holds {size:g} {getattr(pos, 'outcome', '?')} on an "
                            f"unwatched market (token {token_id[:10]}) — manage it manually",
                            level="warning",
                        )
                continue
            pair, leg, outcome = info
            item = self.inventory.get((pair.condition_id, token_id))
            tracked_size = item.shares if (item is not None and item.closed_at is None) else 0.0
            # Drop the flat-bundle portion (already hedged, held to redemption) before measuring
            # what is left unhedged and worth adopting.
            flat = flat_by_token.get(token_id, 0.0)
            delta = size - flat - tracked_size
            if delta >= min_shares:
                avg_price = float(getattr(pos, "avg_price", 0.0))
                if avg_price <= 0:
                    # No cost basis from the data-api: adopting at 0 makes any completion look
                    # like free arb and the bot would buy a real opposite leg. Alert and skip.
                    self._alert_unmanageable_position(
                        pair, outcome, token_id, delta, "unknown cost basis (avgPrice=0)"
                    )
                    continue
                recent = self.settings.reward_mm_position_sync_recent_close_seconds
                if (
                    recent > 0
                    and item is not None
                    and item.closed_at is not None
                    and now - item.closed_at < recent
                ):
                    # This token's inventory closed seconds ago; the data-api is likely still
                    # reporting the just-sold position. Re-adopting it would re-open real
                    # exposure (the run-55 phantom). Skip until the stale read clears.
                    self._alert_unmanageable_position(
                        pair, outcome, token_id, delta,
                        f"closed {now - item.closed_at:.0f}s ago — likely stale data-api",
                    )
                    continue
                self._adopt_inventory(pair, leg, outcome, token_id, delta, avg_price, now)
                adopted += 1
                if self.run_id is not None:
                    self.storage.insert_reward_event(
                        self.run_id,
                        "position_adopted",
                        f"{outcome}: adopted {delta:g} @ {avg_price:.4f} from wallet "
                        f"(tracked {tracked_size:g}, bundle {flat:g} of {size:g})",
                        level="warning",
                    )
        self.metrics.positions_adopted += adopted
        return adopted

    def _alert_unmanageable_position(
        self, pair: RewardPair, outcome: str, token_id: str, shares: float, reason: str
    ) -> None:
        """Warn once that a wallet position on a tracked market was NOT adopted (so the bot will
        not auto-manage it) and must be handled manually."""
        if token_id in self._alerted_untracked:
            return
        self._alerted_untracked.add(token_id)
        if self.run_id is not None:
            self.storage.insert_reward_event(
                self.run_id,
                "position_adopt_skipped",
                f"{outcome}: NOT adopting {shares:g} on {pair.condition_id[:12]} ({reason}) — "
                f"handle manually; bot will not auto-trade it",
                level="error",
            )

    def _opposite_open_inventory(self, plan: RewardQuotePlan) -> RewardInventory | None:
        for item in self._open_inventory_for_pair(plan.condition_id):
            if item.token_id != plan.token_id:
                return item
        return None

    def _mark_inventory(self, pair: RewardPair) -> None:
        assert self.books is not None
        now = time.time()
        for token_id, outcome in ((pair.token_a, pair.outcome_a), (pair.token_b, pair.outcome_b)):
            item = self.inventory.get((pair.condition_id, token_id))
            if item is None or item.shares <= 0:
                continue
            book = self.books.get(token_id)
            bid = book.best_bid() if book else None
            item.mark_price = float(bid) if bid is not None else None
            item.outcome = outcome
            self._store_inventory(item, now)

    def _close_inventory(
        self,
        item: RewardInventory,
        *,
        close_mode: str,
        realized_pnl: float,
        now: float,
        exit_price: float | None = None,
        hedge_price: float | None = None,
        bundle_cost: float | None = None,
        exclude_quote_uid: str | None = None,
        shares_closed: float | None = None,
        risk_reason: str = "",
        trigger_pnl: float | None = None,
        liquidation_pnl_at_trigger: float | None = None,
        hedge_pnl_at_trigger: float | None = None,
        exit_route: str = "",
        effective_max_loss: float | None = None,
    ) -> None:
        shares = min(shares_closed if shares_closed is not None else item.shares, item.shares)
        entry_price = item.avg_price
        item.state = close_mode
        full_close = shares + 1e-12 >= item.shares
        reward_credit = item.reward_credit_usdc if full_close else item.reward_credit_usdc * (
            shares / item.shares
        )
        reward_adjusted_pnl = realized_pnl + reward_credit
        if close_mode == "hedge_locked":
            self.metrics.locked_bundles += 1
            self.metrics.locked_bundle_pnl += realized_pnl
        elif close_mode == "passive_exit":
            self.metrics.passive_exits += 1
            self.metrics.passive_exit_pnl += realized_pnl
        elif close_mode == "emergency_exit":
            self.metrics.emergency_exits += 1
            self.metrics.emergency_exit_pnl += realized_pnl
        elif close_mode == "auto_taker_exit":
            self.metrics.auto_taker_exits += 1
            self.metrics.auto_taker_exit_pnl += realized_pnl
        is_stop = close_mode in ("emergency_exit", "auto_taker_exit")
        loss_cap = effective_max_loss if effective_max_loss is not None else self.max_loss_usdc
        planned_max_loss = loss_cap if is_stop else None
        stop_slippage = min(realized_pnl + loss_cap, 0.0) if is_stop else 0.0
        if is_stop:
            self.metrics.stop_slippage_usdc += stop_slippage
            cooldown = self.settings.reward_mm_post_close_cooldown_seconds
            if cooldown > 0:
                until = now + cooldown
                self.market_cooldowns[item.condition_id] = max(
                    self.market_cooldowns.get(item.condition_id, 0.0), until
                )
        self.storage.insert_reward_inventory_closure(
            {
                "run_id": self.run_id,
                "inventory_uid": item.inventory_uid,
                "condition_id": item.condition_id,
                "token_id": item.token_id,
                "outcome": item.outcome,
                "close_mode": close_mode,
                "shares": shares,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "hedge_price": hedge_price,
                "bundle_cost": bundle_cost,
                "realized_pnl": realized_pnl,
                "reward_credit_usdc": reward_credit,
                "reward_adjusted_pnl": reward_adjusted_pnl,
                "risk_reason": risk_reason,
                "trigger_pnl": trigger_pnl,
                "exit_pnl": realized_pnl,
                "liquidation_pnl_at_trigger": liquidation_pnl_at_trigger,
                "hedge_pnl_at_trigger": hedge_pnl_at_trigger,
                "planned_max_loss_usdc": planned_max_loss,
                "stop_slippage_usdc": stop_slippage,
                "exit_route": exit_route,
                "closed_at": now,
            }
        )
        item.reward_credit_usdc -= reward_credit
        item.total_cost -= entry_price * shares
        item.shares -= shares
        if full_close:
            item.closed_at = now
            item.close_mode = close_mode
            item.closed_pnl = realized_pnl
            item.reward_adjusted_pnl = reward_adjusted_pnl
            self._cancel_inventory_quotes(
                item,
                close_mode,
                now,
                exclude_quote_uid=exclude_quote_uid,
            )
            item.shares = 0.0
            item.total_cost = 0.0
            item.reward_credit_usdc = 0.0
            item.passive_exit_quote_uid = None
            item.passive_exit_price = None
        else:
            item.state = "open_inventory"
        self._store_inventory(item, now, mark=True)

    def _cancel_inventory_quotes(
        self,
        item: RewardInventory,
        reason: str,
        now: float,
        *,
        exclude_quote_uid: str | None = None,
    ) -> None:
        for key, active in list(self.active_quotes.items()):
            if active.quote_uid == exclude_quote_uid:
                continue
            if active.plan.condition_id != item.condition_id:
                continue
            if active.plan.side == "SELL" or active.plan.token_id == item.token_id:
                self.active_quotes.pop(key, None)
                self._request_cancel_quote(
                    active,
                    reason=reason,
                    final_status="cancelled",
                    now=now,
                )
                self.metrics.quote_cancellations += 1

    def _store_inventory(
        self, item: RewardInventory, now: float, *, mark: bool = False
    ) -> None:
        self.storage.upsert_reward_inventory(
            {
                "run_id": self.run_id,
                "condition_id": item.condition_id,
                "token_id": item.token_id,
                "outcome": item.outcome,
                "inventory_uid": item.inventory_uid,
                "state": item.state,
                "shares": item.shares,
                "avg_price": item.avg_price,
                "mark_price": item.mark_price,
                "unrealized_pnl": item.unrealized_pnl,
                "liquidation_pnl": item.liquidation_pnl,
                "reward_credit_usdc": item.reward_credit_usdc,
                "best_hedge_price": item.best_hedge_price,
                "best_hedge_bundle_cost": item.best_hedge_bundle_cost,
                "best_hedge_pnl": item.best_hedge_pnl,
                "passive_exit_quote_uid": item.passive_exit_quote_uid,
                "passive_exit_price": item.passive_exit_price,
                "opened_at": item.opened_at,
                "closed_at": item.closed_at,
                "close_mode": item.close_mode,
                "closed_pnl": item.closed_pnl,
                "reward_adjusted_pnl": item.reward_adjusted_pnl,
                "updated_at": now,
            }
        )
        if mark:
            self.storage.insert_reward_inventory_mark(
                {
                    "run_id": self.run_id,
                    "inventory_uid": item.inventory_uid,
                    "condition_id": item.condition_id,
                    "token_id": item.token_id,
                    "outcome": item.outcome,
                    "state": item.state,
                    "shares": item.shares,
                    "avg_price": item.avg_price,
                    "mark_price": item.mark_price,
                    "liquidation_pnl": item.liquidation_pnl,
                    "reward_credit_usdc": item.reward_credit_usdc,
                    "best_hedge_price": item.best_hedge_price,
                    "best_hedge_bundle_cost": item.best_hedge_bundle_cost,
                    "best_hedge_pnl": item.best_hedge_pnl,
                    "passive_exit_price": item.passive_exit_price,
                    "updated_at": now,
                }
            )

    def _queue_ahead(self, plan: RewardQuotePlan) -> float:
        assert self.books is not None
        book = self.books.get(plan.token_id)
        if book is None:
            return 0.0
        if plan.side == "BUY":
            return sum(
                float(size) for price, size in book.bids.items() if float(price) >= plan.price
            )
        return sum(float(size) for price, size in book.asks.items() if float(price) <= plan.price)

    def _cancel_all_quotes(self, reason: str) -> None:
        now = time.time()
        if self.run_id is not None:
            self.storage.close_open_reward_quotes_for_run(
                self.run_id, status="cancelled", reason=reason, closed_at=now
            )
        else:
            self.storage.close_reward_quotes(
                [
                    active.quote_uid
                    for active in [
                        *self.active_quotes.values(),
                        *self.pending_cancel_quotes.values(),
                    ]
                ],
                status="cancelled",
                reason=reason,
                closed_at=now,
            )
        self.active_quotes.clear()
        self.pending_cancel_quotes.clear()

    def _publish_metrics(self) -> None:
        if self.run_id is None:
            return
        now = time.time()
        self._settle_pending_cancels(now)
        selected_ids = set(self.selected_pairs)
        open_inventory_ids = self._open_inventory_condition_ids()
        live_quotes = self._live_quotes()
        self.metrics.current_open_quotes = len(live_quotes)
        self.metrics.cancel_pending_quotes = len(self.pending_cancel_quotes)
        self.metrics.current_scoring_quotes = sum(
            1 for active in self.active_quotes.values() if active.effective_scoring
        )
        self.metrics.current_non_scoring_quotes = (
            self.metrics.current_open_quotes - self.metrics.current_scoring_quotes
        )
        self.metrics.risk_only_quotes = sum(
            1
            for active in live_quotes
            if active.plan.condition_id not in selected_ids
            and active.plan.condition_id in open_inventory_ids
        )
        self.metrics.stale_open_quotes = sum(
            1
            for active in live_quotes
            if selected_ids
            and active.plan.condition_id not in selected_ids
            and active.plan.condition_id not in open_inventory_ids
        )
        self.metrics.cooldown_markets = sum(
            1 for until in self.market_cooldowns.values() if until > now
        )
        self.metrics.game_paused_markets = sum(
            1
            for pair in self.selected_pairs.values()
            if self._market_in_game_window(pair, now)
        )
        measured_competitor = [
            self.market_competitor_score[cid]
            for cid in self.selected_pairs
            if cid in self.market_competitor_score
        ]
        self.metrics.competitor_q_avg = (
            sum(measured_competitor) / len(measured_competitor)
            if measured_competitor
            else 0.0
        )
        warmup = self.settings.reward_mm_warmup_seconds
        self.metrics.warmup_blocked_markets = sum(
            1
            for cid in self.selected_pairs
            if (fs := self.market_first_seen.get(cid)) is not None and now - fs < warmup
        )
        band = self.settings.reward_mm_stability_band
        unstable = 0
        paying = 0
        min_payout = self.settings.reward_mm_min_payout_usdc
        haircut = self.settings.reward_mm_competitor_haircut_base
        cap_base = self.settings.reward_mm_liquidity_share_cap_base
        for cid, pair in self.selected_pairs.items():
            if band > 0 and self.books is not None:
                ema = self.market_midpoint_ema.get(cid)
                mids = self._pair_midpoints(pair)
                if ema is not None and mids is not None and (
                    max(abs(mids[0] - ema[0]), abs(mids[1] - ema[1])) > band
                ):
                    unstable += 1
            scoring = [
                active
                for active in self.active_quotes.values()
                if active.plan.condition_id == cid and active.effective_scoring
            ]
            if scoring:
                _q1, _q2, q_min = self._liquidity_reward_scores(pair, scoring, now)
                competitor = self.market_competitor_score.get(cid, 0.0) * haircut
                daily = self._liquidity_reward_increment(
                    pair.rewards_rate_per_day, q_min, 86_400.0, competitor, cap_base
                )
                if daily >= min_payout:
                    paying += 1
        self.metrics.unstable_markets = unstable
        self.metrics.markets_paying_reward = paying
        drift_limit = self.settings.reward_mm_trend_drift
        drift_window = self.settings.reward_mm_trend_window_seconds
        self.metrics.trending_markets = (
            sum(
                1
                for cid in self.selected_pairs
                if self._window_drift(cid, now=now, window_seconds=drift_window) >= drift_limit
            )
            if drift_limit > 0 and drift_window > 0
            else 0
        )
        long_window = self.settings.reward_mm_runtime_volatility_window_seconds
        range_limit = self.settings.reward_mm_runtime_volatility_range
        jump_window = self.settings.reward_mm_runtime_jump_window_seconds
        jump_limit = self.settings.reward_mm_runtime_jump_move
        self.metrics.runtime_volatile_markets = sum(
            1
            for cid in self.selected_pairs
            if (
                range_limit > 0
                and long_window > 0
                and self._window_range(cid, now=now, window_seconds=long_window) >= range_limit
            )
            or (
                jump_limit > 0
                and jump_window > 0
                and self._window_range(cid, now=now, window_seconds=jump_window) >= jump_limit
            )
        )
        open_inventory = [
            item
            for item in self.inventory.values()
            if item.shares > 0 and item.closed_at is None
        ]
        self.metrics.inventory_positions = len(open_inventory)
        self.metrics.inventory_exposure = sum(
            item.avg_price * item.shares for item in open_inventory
        )
        self.metrics.inventory_unrealized_pnl = sum(
            item.unrealized_pnl or 0.0 for item in open_inventory
        )
        risk = self._inventory_risk_summary()
        self.metrics.matched_bundle_positions = int(risk["matched_bundle_positions"])
        self.metrics.matched_bundle_shares = risk["matched_bundle_shares"]
        self.metrics.matched_bundle_pnl = risk["matched_bundle_pnl"]
        self.metrics.unhedged_shares = risk["unhedged_shares"]
        self.metrics.unhedged_pnl = risk["unhedged_pnl"]
        self.metrics.inventory_state_errors = sum(
            1
            for item in self.inventory.values()
            if item.shares > 0 and item.closed_at is not None
        )
        inventory_and_closed_pnl = (
            self.metrics.locked_bundle_pnl
            + self.metrics.passive_exit_pnl
            + self.metrics.emergency_exit_pnl
            + self.metrics.auto_taker_exit_pnl
            + self.metrics.inventory_unrealized_pnl
        )
        self.metrics.reward_net_conservative_usdc = (
            inventory_and_closed_pnl
            + self.metrics.liquidity_reward_conservative_usdc
            + self.metrics.maker_rebate_conservative_usdc
        )
        self.metrics.reward_net_base_usdc = (
            inventory_and_closed_pnl
            + self.metrics.liquidity_reward_base_usdc
            + self.metrics.maker_rebate_base_usdc
        )
        self.metrics.reward_net_optimistic_usdc = (
            inventory_and_closed_pnl
            + self.metrics.liquidity_reward_optimistic_usdc
            + self.metrics.maker_rebate_optimistic_usdc
        )
        self.metrics.reward_adjusted_net_pnl = self.metrics.reward_net_base_usdc
        self._refresh_live_readiness()
        for key, value in asdict(self.metrics).items():
            self.storage.set_reward_metric(self.run_id, key, float(value))

    def _refresh_live_readiness(self) -> None:
        if not self.settings.reward_mm_live_shadow_enabled:
            self.metrics.live_readiness_passed = 0
            self.metrics.live_readiness_blockers = 1
            return
        blockers: list[str] = []
        if (
            self.metrics.reward_net_conservative_usdc
            < self.settings.reward_mm_live_min_conservative_net_usdc
        ):
            blockers.append("conservative_net_below_min")
        if self.metrics.stale_open_quotes > self.settings.reward_mm_live_max_stale_quotes:
            blockers.append("stale_quotes")
        if self.metrics.inventory_state_errors > 0:
            blockers.append("inventory_state_errors")
        if self.metrics.emergency_exits > 0:
            blockers.append("emergency_exits")
        if (
            self.metrics.stop_slippage_usdc
            < -abs(self.settings.reward_mm_live_max_stop_slippage_usdc)
        ):
            blockers.append("stop_slippage")
        fill_rate = (
            self.metrics.fill_before_cancel / self.metrics.would_have_fills
            if self.metrics.would_have_fills > 0
            else 0.0
        )
        self.metrics.live_readiness_fill_before_cancel_rate = fill_rate
        if fill_rate > self.settings.reward_mm_live_max_fill_before_cancel_rate:
            blockers.append("fill_before_cancel_rate")
        current = tuple(blockers)
        self.metrics.live_readiness_passed = 1 if not blockers else 0
        self.metrics.live_readiness_blockers = len(blockers)
        if (
            self.run_id is not None
            and current != self.last_live_readiness_blockers
            and (current or self.last_live_readiness_blockers)
        ):
            self.storage.insert_reward_event(
                self.run_id,
                "live_readiness",
                "passed" if not blockers else ", ".join(blockers),
                level="info" if not blockers else "warning",
                payload={"blockers": blockers},
            )
        self.last_live_readiness_blockers = current

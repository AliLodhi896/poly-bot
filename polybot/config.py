from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Engine settings for reward market making.

    Every field has a default the bot has actually run with, so a fresh checkout works with
    no ``.env`` at all. Overrides are read from the environment with the ``POLYBOT_`` prefix.
    """

    model_config = SettingsConfigDict(
        env_prefix="POLYBOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gamma_host: str = "https://gamma-api.polymarket.com"
    clob_host: str = "https://clob.polymarket.com"
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    # The official geoblock endpoint sits behind Cloudflare and can return a bot-challenge (403)
    # that no header set passes. When that happens, verify the location independently via IP
    # geolocation against Polymarket's restricted-jurisdiction list instead of crashing the start.
    # The compliance check is preserved — it just no longer depends on one reachable endpoint.
    geoblock_fallback_enabled: bool = True
    geoblock_ip_lookup_url: str = "https://ipwho.is/"
    # Polymarket fully-blocked + close-only jurisdictions (ISO-3166 alpha-2). Close-only
    # (PL,SG,TH,TW) are included because the reward bot OPENS maker orders, which close-only does
    # not permit. Source: docs.polymarket.com/api-reference/geoblock — update if Polymarket changes
    # its list. Blocked sub-regions (CA Ontario, UA Crimea/Donetsk/Luhansk) are handled separately.
    geoblock_blocked_countries: str = (
        "AU,BE,BY,BI,CF,CD,CU,DE,ET,FR,GB,IR,IQ,IT,KP,LB,LY,MM,NI,NL,RU,SO,SS,SD,SY,UM,US,VE,YE,ZW,"
        "PL,SG,TH,TW"
    )
    market_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    user_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    request_timeout_seconds: float = 12.0
    max_retries: int = 2

    database_path: Path = Path("polybot.sqlite3")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080

    ws_batch_size: int = 240
    heartbeat_seconds: float = 1.0

    reward_mm_market_limit: int = 50
    reward_mm_size_shares: float = 100.0
    reward_mm_refresh_seconds: float = 60.0
    reward_mm_quote_distance_fraction: float = 0.5
    reward_mm_snapshot_interval_seconds: float = 5.0
    reward_mm_assumed_reward_share: float = 0.02
    reward_mm_single_sided_score_factor: float = 3.0
    # Ceiling on our estimated share of a market's reward pool (estimation only — never gates
    # orders). The old 0.02 was far too low: calibrated against real payouts (weather 35sh on a
    # $100/day pool earned $2.5 = 2.5% share; a 50sh 5-market overnight run realised ~8% avg
    # share, vs the 2%-capped model that under-reported it ~4x). The share formula
    # q_self/(q_self+competition) reproduces both once the cap stops clipping it; the cap now only
    # bounds the degenerate near-empty-book case where the snapshot misses intermittent makers.
    reward_mm_liquidity_share_cap_conservative: float = 0.10
    reward_mm_liquidity_share_cap_base: float = 0.20
    reward_mm_liquidity_share_cap_optimistic: float = 0.40
    reward_mm_liquidity_competitor_score_conservative: float = 25_000.0
    reward_mm_liquidity_competitor_score_base: float = 5_000.0
    reward_mm_liquidity_competitor_score_optimistic: float = 1_000.0
    reward_mm_maker_rebate_rate_conservative: float = 0.0
    reward_mm_maker_rebate_rate_base: float = 0.05
    reward_mm_maker_rebate_rate_optimistic: float = 0.25
    reward_mm_cancel_latency_ms: float = 300.0
    reward_mm_live_shadow_enabled: bool = True
    reward_mm_live_min_conservative_net_usdc: float = 0.0
    reward_mm_live_max_fill_before_cancel_rate: float = 0.05
    reward_mm_live_max_stop_slippage_usdc: float = 0.0
    reward_mm_live_max_stale_quotes: int = 0
    reward_mm_micro_live_enabled: bool = False
    reward_mm_micro_live_max_markets: int = 1
    # Live execution (off until LIVE_ENABLED + MICRO_LIVE_ENABLED). Balance-bound cap on
    # total resting BUY notional, and the one-order place+cancel dry-run that farming requires.
    reward_mm_live_max_balance_usdc: float = 300.0
    reward_mm_live_require_dry_run: bool = True
    reward_mm_live_dry_run_marker: Path = Path(".polybot-dry-run")
    # Fallback period for the (event-driven) reconcile loop — how long it waits for a wake before
    # re-syncing anyway. 0.25s makes non-urgent moves (e.g. a toward-mid depth reprice) and the
    # periodic sync snappy; a reconcile with no price change places nothing (no network), so a
    # stable book adds no churn. Well within CLOB limits (POST/DELETE order 5,000/10s; the
    # balance read is cached, not a GET per tick), and batched (one cancel + one place request).
    reward_mm_live_reconcile_seconds: float = 0.25
    # Event-driven reconcile: the engine wakes the loop the instant it pulls a quote for an
    # adverse reason or books a fill. Safety wakes are marked ``urgent`` and BYPASS this floor so
    # an endangered order is cancelled with minimal latency. The floor only paces routine wakes;
    # since the balance read is cached (not a per-tick GET) and order ops are batched, it can sit
    # far below the old 0.15 — order/cancel budget is 5,000/10s, so 0.05 stays well under it.
    reward_mm_live_min_reconcile_seconds: float = 0.05
    # Client-side dead-man switch: if the WHOLE market feed goes silent this long (a real WS
    # disconnect/stall — not a single calm market, since the feed is shared across all tokens),
    # the live reconcile pulls every resting order so we don't quote blind. Must be well above
    # normal inter-message gaps or it would cancel orders on quiet-but-connected markets and
    # earn nothing. 0 disables the guard.
    reward_mm_live_feed_stale_seconds: float = 20.0
    # Live-only BUY chase guard: when the market moves up, the normal reward planner may want
    # to lift our BUY closer to mid for a better score. If the old BUY is still scoring, keep it
    # resting for at least this long (and at least the market's minimum order age) before chasing
    # upward. This preserves the reward strategy without repeatedly moving into fresh fill risk.
    # 0 disables the delay and restores immediate upward chasing.
    reward_mm_live_buy_chase_delay_seconds: float = 30.0
    # Keep the reconcile hot path free of blocking gateway round-trips (they freeze the single
    # event loop and delay the price feed — worse now that reconcile is event-driven). The
    # orphan sweep runs on its own slow loop (and on demand after an ambiguous batch error)
    # instead of every reconcile; the balance read is cached for a few seconds; a just-cancelled
    # orphan id is not re-cancelled for a cooldown. All 0 = disable that piece.
    reward_mm_live_orphan_sweep_seconds: float = 15.0
    reward_mm_live_balance_cache_seconds: float = 3.0
    reward_mm_live_orphan_recancel_cooldown_seconds: float = 10.0
    reward_mm_live_orphan_token_ttl_seconds: float = 60.0
    # Send the reconcile's cancels/places as batched CLOB requests (one round-trip each) instead
    # of one per order, so many markets don't freeze the event loop. False falls back to the
    # proven per-order placement path (cancels are always batched — that path is unconditionally
    # safe). Mistracked/duplicate orders are still caught by the orphan reaper.
    reward_mm_live_batch_orders: bool = True
    # Inventory↔wallet reconciliation: periodically read the wallet's real on-chain positions
    # (Polymarket data-api) and ADOPT any the bot under-tracks on a market it is trading (e.g. a
    # fill that was dropped) so they are managed and the PnL is accurate. Adopt-only (never auto-
    # reduce, since the data-api can lag a just-recorded fill); off-universe positions are alerted,
    # not adopted. 0 disables.
    reward_mm_live_position_sync_seconds: float = 60.0
    reward_mm_position_sync_min_shares: float = 5.0
    # Do not adopt a wallet position on a token whose inventory item closed within this many
    # seconds: the data-api can still report a just-sold position, and re-adopting it (then
    # "managing" it) re-opens real exposure. 0 disables the guard.
    reward_mm_position_sync_recent_close_seconds: float = 180.0
    # If the user channel reports a maker fill for an order id the coordinator does not track,
    # halt and cancel live orders. That state is too ambiguous for unattended real-money trading.
    reward_mm_live_stop_on_untracked_fill: bool = True
    reward_mm_max_loss_usdc: float = 1.0
    # Unattended-safety: when a market hits its stop-loss, retire it for the rest of the run
    # (cancel its orders, quote nothing, hold the open position to resolution) instead of the
    # default cancel-orders + 60s-cooldown + recovery-quoting. Off by default; resets on restart.
    reward_mm_halt_market_on_stop_loss: bool = False
    reward_mm_max_position_shares: float = 500.0
    # Entry exposure cap in USDC, applied PER-MARKET: it bounds the working notional (open
    # inventory + fresh entry quotes) of each market independently. A fill on one market therefore
    # never shrinks the quoting budget of the others. Two complementary legs of N shares always
    # cost N x $1, so this must be >= size_shares to quote both sides of a market.
    reward_mm_max_exposure_usdc: float = 1000.0
    # Gap-guard: pause quoting after a fast single jump. Widened to 4c so normal repricing
    # noise on the calm markets the scanner already vets doesn't keep dropping them, while a
    # genuine sharp jump still pauses. Cooldown shortened to 45s so a market recovers faster.
    reward_mm_gap_guard_move: float = 0.04
    reward_mm_gap_guard_window_seconds: float = 5.0
    reward_mm_gap_guard_cooldown_seconds: float = 45.0
    # Risk-adjusted market selection (Phase 0.1).
    # Off by default: the daily price-change filter also excluded calm-now esports (where the
    # bundle income forms). Intraday/live risk is handled by the trend detector and gap-guard.
    reward_mm_max_one_day_change: float = 0.0
    reward_mm_min_top_depth_shares: float = 200.0
    reward_mm_min_midpoint: float = 0.10
    reward_mm_priority_vol_k: float = 5.0
    # Phase A (live branch): rank markets by activity, not low-competition. "volume" targets
    # two-sided active markets (esports/sports) where the YES+NO bundle income forms; "rate"
    # mirrors the old code; "projected" keeps the competition-aware ranking.
    reward_mm_selection_mode: Literal["volume", "rate", "projected"] = "volume"
    # Skip markets whose reward min order size exceeds this (0 = off). Lets a small bankroll
    # quote only cheap markets, e.g. 50 => only markets needing <=50 shares/leg.
    reward_mm_max_min_size_shares: float = 0.0
    # Runtime entry-size depth guard. 1 = quote size up to visible top-of-book depth;
    # 2 = require roughly 2x visible top depth for the configured quote size.
    reward_mm_entry_depth_multiplier: float = 1.0
    # Depth-based quote placement — ON by default, and the main thing separating this bot from a
    # loop that quotes at a fixed offset. A BUY is placed at the tick closest to midpoint whose
    # cumulative book depth "ahead" of us is nearest a target buffer of shares, so a single taker
    # cannot reach the order without first eating that buffer. The buffer is keyed to the market's
    # reward min_shares via the per-tier schedule in polybot/rewards/planner.py (DEPTH_TIER_MULTS)
    # — edit that table to retune. The market browser already grades markets in this mode, so
    # turning it off makes the bot quote differently from the list you picked from.
    # Set false only to reproduce the legacy fixed-fraction-of-reward-spread placement.
    reward_mm_depth_based_placement: bool = True
    # Live game-window pause (Phase 0.2).
    reward_mm_game_pause_enabled: bool = True
    reward_mm_game_pause_pre_seconds: float = 900.0
    reward_mm_game_pause_post_seconds: float = 14_400.0
    # Deprecated: runtime sizing no longer shrinks entries from one-day price change. Keep the
    # env knob accepted for older configs; live volatility is handled by the guards below.
    reward_mm_size_vol_k: float = 5.0
    # The stop-loss is one number, chosen per run: reward_mm_max_loss_usdc (the launch screen's
    # "max loss / position"). Earlier per-share variants that scaled or duplicated it are gone —
    # a second stop that silently overrides the operator's choice is a footgun, not a feature.
    # Adaptive quote distance.
    reward_mm_distance_vol_widen_k: float = 4.0
    # Scratch/trailing passive exit: works the position back to flat, not an emergency stop.
    reward_mm_scratch_loss_per_share: float = 0.01
    # Off by default: this was a portfolio-wide block — a single position above the band halted
    # new quotes in ALL markets. Portfolio risk is already bounded by max_exposure_usdc and
    # max_position_shares. Set > 0 only to cap aggregate unhedged exposure.
    reward_mm_max_net_delta_shares: float = 0.0
    reward_mm_complete_leg_distance_fraction: float = 0.1
    # Post-fill leg management (Phase 1.3).
    # Immediate arb-completion trigger: buy the opposite leg as a taker when the resulting set
    # cost (avg + opp_ask + taker fee) is at or below this ceiling. Fixed and absolute on
    # purpose — it is decoupled from accrued reward credit so banked rewards never justify
    # voluntarily realising a large completion loss. 1.010 = tolerate ~1c of true loss for an
    # immediate, guaranteed close.
    reward_mm_arb_close_max_cost: float = 1.010
    # Master switch for post-fill arb completion. On (default) preserves current behaviour: after a
    # fill the bot immediately looks to buy the opposite leg and lock the set (both the immediate
    # taker bundle-lock and the resting completion buy). Off skips the arb check entirely and the
    # filled leg is managed single-legged (stop-loss, soft-stop, passive sell-to-flat) unchanged.
    reward_mm_arb_close_after_fill: bool = True
    # Live small-size auto-taker exit on stop-loss. 0 = disabled (always alert for manual exit,
    # legacy behaviour). When > 0, a stopped position no larger than this many shares (and, if
    # set below, no larger than the notional cap) is cut automatically with a price-capped FOK
    # taker on the cheaper of (sell the held leg / complete the set); larger positions still
    # alert for a manual exit.
    reward_mm_live_auto_exit_max_shares: float = 0.0
    reward_mm_live_auto_exit_max_notional_usdc: float = 0.0
    # Slippage cap for the auto-taker exit FOK: sell floor = best_bid - slip, buy cap =
    # opp_ask + slip. Bounds how far the fill may be from the touch before the FOK is killed.
    reward_mm_live_auto_exit_slippage: float = 0.01
    # Min seconds between stop-loss actions (auto-exit attempt / emergency alert) on one market.
    # A just-filled position's shares may not be settled yet, so the first FOK can fail
    # (balance:0) and the stop re-fires every tick — this throttles the retries/alerts to a
    # sane cadence while still recovering once the shares settle. 0 disables the throttle.
    reward_mm_stop_action_throttle_seconds: float = 3.0
    # After any fill on a market, block NEW entries there for the rest of the run (in-memory, so a
    # restart re-enables it) while the post-fill race/stop/auto-exit keeps managing the open leg.
    # A market that just adversely filled is likely informed/volatile, so we do not re-enter it
    # this run; other markets keep trading normally.
    reward_mm_retire_market_after_fill: bool = True
    # Reward-estimate realism (Phase 2).
    reward_mm_competitor_from_book: bool = True
    reward_mm_competitor_haircut_conservative: float = 1.5
    reward_mm_competitor_haircut_base: float = 1.0
    reward_mm_competitor_haircut_optimistic: float = 0.66
    # Snapshot/DB retention (Phase 2.3).
    reward_mm_snapshot_retention_seconds: float = 7_200.0
    # Round 2 — anti-sniping. Defaulted off in round 3: entry cooldowns suppressed
    # profitable bundle completion without reducing fill-before-cancel. Kept tunable.
    reward_mm_warmup_seconds: float = 0.0
    reward_mm_midpoint_ema_alpha: float = 0.3
    reward_mm_stability_band: float = 0.0
    reward_mm_post_close_cooldown_seconds: float = 120.0
    # Round 4 — trend/drift detector: pause entries when the midpoint moves directionally
    # (live match / news gap), but not when it just oscillates (choppy = bundle income).
    # Widened 4c -> 6c: a 4c drift over 30s is within normal repricing for the markets the
    # scanner already vets, and was pausing them on non-toxic noise; a real directional run
    # still trips it.
    reward_mm_trend_window_seconds: float = 30.0
    reward_mm_trend_drift: float = 0.06
    # Runtime volatility guards: pause a market when the live midpoint range gets too wide.
    # These use current WS/order-book movement, not one-day historical change. The 10-minute
    # range backstop (0.20) is deliberately left tight — it is the protection against the sharp
    # moves we never want to quote through. Only the fast-jump trip is eased (5c -> 7c).
    reward_mm_runtime_volatility_window_seconds: float = 600.0
    reward_mm_runtime_volatility_range: float = 0.20
    reward_mm_runtime_jump_window_seconds: float = 60.0
    reward_mm_runtime_jump_move: float = 0.07
    # 0 disables the launch/refresh prices-history check; live WS guards still run.
    reward_mm_runtime_history_timeout_seconds: float = 0.75
    # Dashboard scanner volatility/risk model. Runtime guards remain separate.
    reward_mm_scanner_volatility_10m_warn: float = 0.06
    reward_mm_scanner_volatility_10m_reject: float = 0.12
    reward_mm_scanner_volatility_2m_warn: float = 0.02
    reward_mm_scanner_volatility_2m_reject: float = 0.03
    # Max Gamma /events scanned in the bulk category sweep. The sweep pages events by volume and
    # stops early once every reward event is matched, so this is just a safety ceiling on how deep
    # it will page before falling back to keyword classification for any still-unmatched tail.
    reward_mm_scanner_metadata_event_limit: int = 6000
    # Latent-competition floor for the share estimate. An empty book within spread at scan time
    # (competition ~0) does NOT mean we would capture ~100% of the pool — it usually means the
    # snapshot missed intermittent makers or the market is inactive. Floor the competition at the
    # minimum realistically-faced level (calibrated to the ~144 implied by a real 50sh run that
    # earned ~8% share) so projected_reward does not blow up on near-empty books.
    reward_mm_scanner_competition_floor: float = 150.0
    # Reward-realism discount (constant multiplier on projected_reward; does not change ranking).
    # Default 1.0 = no discount: an earlier 0.20 was set from a bad data point (a 1.5-min warmup
    # window where orders had not yet reached minimum_order_age and so under-scored). Only set
    # this below 1.0 after calibrating against real payouts (get_reward_percentages). On
    # low-competition markets the model UNDER-estimates (the 2% share cap caps a share that can
    # legitimately be much higher when you are the dominant LP), so a blanket discount is wrong.
    reward_mm_scanner_reward_realism_factor: float = 1.0
    # Sweep-resistance: protective resting shares between the midpoint and our quote price — the
    # size a single adverse order must consume before it reaches us. It is a *soft* modifier, not
    # the lead signal: the scanner ranks by reward (which already prices in competition), and
    # sweep only nudges. The factor ranges [sweep_factor_floor, 1.0] and saturates at
    # `target = max(sweep_target_base, sweep_target_size_mult * desired_size)` — the target scales
    # with our quote size because protection only needs to dwarf what *we* risk, not a fixed wall.
    # A thin book is dangerous only when price moves through it, and that is caught by the
    # volatility gate; on a calm low-competition market a thin book is the best farm (big share,
    # no fills). `sweepable` badge is raised below `sweep_floor_mult * desired_size`.
    reward_mm_scanner_sweep_target_base: float = 120.0
    reward_mm_scanner_sweep_target_size_mult: float = 4.0
    reward_mm_scanner_sweep_factor_floor: float = 0.45
    reward_mm_scanner_sweep_floor_mult: float = 1.0
    # Order-book imbalance (protective bid vs ask within the quote band). Beyond this skew the
    # midpoint is likely to drift into the thin side and take us; raises an `imbalanced` badge
    # and a soft penalty. 0 disables.
    reward_mm_scanner_imbalance_warn: float = 0.80
    # Round 2/3 — curated market selection.
    reward_mm_market_whitelist: str = ""
    reward_mm_min_hours_to_resolution: float = 0.0
    # Empty by default: esports is the main income source (bundle profits). Live-match toxicity
    # is handled by the trend detector, not by blanket keyword exclusion. Set to block specific
    # categories manually if desired.
    reward_mm_exclude_keywords: str = ""
    reward_mm_select_min_payout_usdc: float = 0.0
    # Round 2 — reward-estimate accuracy.
    reward_mm_dust_size_cutoff: float = 0.0
    reward_mm_min_payout_usdc: float = 1.0

    # --- Safety-first market selector (polybot/rewards/selector.py). All hard gates: a market
    # that fails ANY gate is excluded outright; reward never buys back risk. Survivors are ranked
    # by projected reward (tie-break: safety score). All thresholds are tunable here. ---
    reward_mm_selector_min_price: float = 0.10  # exclude mid outside [p, 1-p] (extreme legs)
    reward_mm_selector_min_hours: float = 18.0  # exclude < this many hours to resolution
    # Weather converges gradually (temperature, not a single headline) and its higher-paying tier
    # lives at 12-18h to resolution, so it gets a shorter near-resolution gate than other cats.
    reward_mm_selector_min_hours_weather: float = 12.0
    reward_mm_selector_max_one_day_change: float = 0.15  # exclude |1-day move| above this
    reward_mm_selector_vol_range_10m_max: float = 0.08  # exclude 10m realized range above this
    reward_mm_selector_vol_step_2m_max: float = 0.04  # exclude 2m max consecutive step above this
    reward_mm_selector_sweep_target_lots: float = 20.0  # in-zone depth / min_size for full marks
    # Page (cheaply, no book fetch) the whole reward pool so the cheap safety gates see the entire
    # universe (~15k markets), not just the top-1000 by rate. Paging is ~one GET per 500 markets.
    reward_mm_selector_pool_limit: int = 20000
    # Hydration (book + volatility fetch) is the expensive step, so it is bounded AND stratified by
    # category: each category gets up to this many of its top-by-rate cheap survivors, so the safest
    # low-rate categories (weather, finance up/down) are not starved by high-rate ones.
    reward_mm_selector_hydrate_per_category: int = 60
    # Hard ceiling on total markets hydrated per scan (across all categories).
    reward_mm_selector_hydrate_limit: int = 600
    # Volatility (prices-history) is the slow API, so it is fetched only for the top reward
    # contenders per category (the rest keep a 'vol unknown' middling safety). Bounds the scan time.
    reward_mm_selector_vol_check_per_category: int = 30
    reward_mm_selector_vol_concurrency: int = 16
    reward_mm_selector_vol_timeout_seconds: float = 2.0
    # Paging the whole ~20k-market pool is slow (~3 min), so the parsed pool is cached and
    # refreshed in the background (stale-while-revalidate): the first scan warms it, every later
    # scan reads it instantly while a refresh runs behind. The pool composition changes slowly.
    reward_mm_selector_pool_cache_seconds: float = 300.0
    # Size-fit tie-breaker: a small EXACT-match nudge. Only markets whose reward min_size equals
    # the configured quote size (50->50, 100->100, …) get their ranking reward scaled by (1 + k);
    # every other tier keeps its raw reward. Exact-fit markets pay more and fully use the size
    # (verified live). Order within a tier is preserved. 0 disables.
    reward_mm_selector_size_fit_k: float = 0.25
    # exclude sports starting within this many seconds
    reward_mm_selector_sports_pre_seconds: float = 1800.0
    # exclude sports up to this many seconds after start
    reward_mm_selector_sports_post_seconds: float = 14400.0

    # The executor is retained as a hard-gated safety shell, not as a product mode.
    live_enabled: bool = False
    kill_switch: bool = False
    kill_switch_file: Path = Path(".polybot-kill")
    chain_id: int = 137
    wallet_private_key: str | None = None
    wallet_funder_address: str | None = None
    # 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, 3=POLY_1271. The deposit-wallet flow in
    # docs/LIVE.md — the only documented way to fund this bot — signs as 3.
    wallet_signature_type: int = 3
    # Optional shared token guarding the control API (start/stop). Empty = open (loopback
    # single-operator default). Set POLYBOT_CONTROL_TOKEN when exposing beyond localhost.
    control_token: str | None = None
    # Cross-process lock so only one live bot can run against the wallet at a time
    # (held by `reward-mm-live`, checked by the control manager before it spawns).
    live_lock_file: Path = Path(".polybot-live.lock")
    # Drop py_clob_client_v2's transient "request error: <ConnectionTerminated/timeout>"
    # ERROR lines from the terminal. The live loop already catches these and records
    # reward_events rows; this only hides the duplicate library noise, not real API
    # status/body errors. Set false when debugging CLOB connectivity.
    quiet_clob_v2_request_errors: bool = True

    @property
    def is_paper_only(self) -> bool:
        return not self.live_enabled

    @property
    def reward_mm_whitelist(self) -> set[str]:
        return {
            item.strip() for item in self.reward_mm_market_whitelist.split(",") if item.strip()
        }

    @property
    def reward_mm_exclude_keyword_set(self) -> set[str]:
        return {
            kw.strip().lower()
            for kw in self.reward_mm_exclude_keywords.split(",")
            if kw.strip()
        }

    @property
    def geoblock_blocked_country_set(self) -> set[str]:
        return {c.strip().upper() for c in self.geoblock_blocked_countries.split(",") if c.strip()}

def get_settings() -> Settings:
    return Settings()

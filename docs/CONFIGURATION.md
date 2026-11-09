# Configuration reference

Polybot has three layers of configuration, and most people never touch the third.

1. **`.env`** — secrets, the live switches, and where the local server listens. See
   [`.env.example`](../.env.example); it is short on purpose.
2. **The launch screen** — what changes from run to run: which markets, position size, max loss,
   duration, and two behaviour toggles. These are the numbers that matter, and they are chosen in
   the UI, not in a file.
3. **Everything below** — strategy tuning. Every value is a calibrated default that the bot has
   actually run with. Override any of them by setting the environment variable in `.env`.

> Defaults live in `polybot/config.py` and nowhere else. If a value here disagrees with the code,
> the code is right — please open an issue.

---

## What the launch screen controls

You do not need environment variables for these. Setting them in `.env` only changes the starting
value of the corresponding control.

| Control | Variable | Default |
| --- | --- | --- |
| Selected markets | `POLYBOT_REWARD_MM_MARKET_WHITELIST` | *(auto-pick)* |
| Markets to quote | `POLYBOT_REWARD_MM_MARKET_LIMIT` | 50 |
| Position size (shares/side) | `POLYBOT_REWARD_MM_SIZE_SHARES` | 100 |
| Max loss per position | `POLYBOT_REWARD_MM_MAX_LOSS_USDC` | 1.0 |
| Instant arbitrage on fill | `POLYBOT_REWARD_MM_ARB_CLOSE_AFTER_FILL` | true |
| Halt market on stop-loss | `POLYBOT_REWARD_MM_HALT_MARKET_ON_STOP_LOSS` | false |

There is exactly one stop-loss, and it is the one you set here. Earlier per-share variants that
scaled or shadowed it have been removed — a second, invisible stop is a footgun.

---

## Quote placement

The part that most distinguishes this bot from a fixed-offset quoting loop.

| Variable | Default | What it does |
| --- | --- | --- |
| `POLYBOT_REWARD_MM_DEPTH_BASED_PLACEMENT` | `true` | Place at the tick closest to midpoint whose cumulative depth *ahead* is nearest a target buffer, so a single taker cannot reach the order without eating that buffer first. Set `false` for the legacy fixed-fraction-of-reward-spread placement. |
| `POLYBOT_REWARD_MM_ENTRY_DEPTH_MULTIPLIER` | `1.0` | Depth required per unit of quote size (legacy mode's size cap). |
| `POLYBOT_REWARD_MM_QUOTE_DISTANCE_FRACTION` | `0.5` | Legacy mode: distance from mid as a fraction of the reward spread. |
| `POLYBOT_REWARD_MM_DISTANCE_VOL_WIDEN_K` | `4.0` | Widen quote distance on volatile markets. |
| `POLYBOT_REWARD_MM_DUST_SIZE_CUTOFF` | `0.0` (= market min size) | Ignore orders below this size when computing the midpoint and competitor liquidity. |
| `POLYBOT_REWARD_MM_COMPLETE_LEG_DISTANCE_FRACTION` | `0.1` | Distance for the completing (opposite-leg) quote. |

The depth buffer itself is a table, not a setting: `DEPTH_TIER_MULTS` in
`polybot/rewards/planner.py` maps a market's reward `min_shares` to floor/target/ceiling
multipliers. Edit that table to retune depth placement.

## Market selection

| Variable | Default | What it does |
| --- | --- | --- |
| `POLYBOT_REWARD_MM_SELECTION_MODE` | `volume` | `volume` targets two-sided active markets; `rate` reproduces raw `rate_per_day` ordering; `projected` ranks by competition-aware projection. |
| `POLYBOT_REWARD_MM_MIN_MIDPOINT` | `0.10` | Reject near-0/1 markets. |
| `POLYBOT_REWARD_MM_MIN_TOP_DEPTH_SHARES` | `200` | Reject dead books (legacy mode only — depth mode uses buffer feasibility). |
| `POLYBOT_REWARD_MM_MAX_ONE_DAY_CHANGE` | `0.0` (off) | Reject markets that moved more than this in a day. |
| `POLYBOT_REWARD_MM_MIN_HOURS_TO_RESOLUTION` | `0.0` | Skip near-settlement markets. |
| `POLYBOT_REWARD_MM_MAX_MIN_SIZE_SHARES` | `0.0` (off) | Restrict to markets whose reward min size is affordable. |
| `POLYBOT_REWARD_MM_EXCLUDE_KEYWORDS` | *(empty)* | Comma-separated keyword blocklist. |
| `POLYBOT_REWARD_MM_MIN_PAYOUT_USDC` | `1.0` | Polymarket's daily floor — a day below it pays nothing. |
| `POLYBOT_REWARD_MM_PRIORITY_VOL_K` | `5.0` | How much volatility discounts a market's rank. |
| `POLYBOT_REWARD_MM_REFRESH_SECONDS` | `60` | Universe refresh cadence. |

### Grades and filters on the market browser

These drive the A/B/C grade, the "risky" toggle and the filter chips on the launch screen. Change
them and the list you pick from changes.

| Variable | Default | Feeds |
| --- | --- | --- |
| `POLYBOT_REWARD_MM_SELECTOR_MIN_PRICE` | `0.10` | `extreme_price` |
| `POLYBOT_REWARD_MM_SELECTOR_MIN_HOURS` | `18.0` | `near_resolution` |
| `POLYBOT_REWARD_MM_SELECTOR_MIN_HOURS_WEATHER` | `12.0` | `near_resolution` (weather) |
| `POLYBOT_REWARD_MM_SELECTOR_MAX_ONE_DAY_CHANGE` | `0.15` | `volatile_1d` |
| `POLYBOT_REWARD_MM_SELECTOR_VOL_RANGE_10M_MAX` | `0.08` | `volatile`, "calm" score |
| `POLYBOT_REWARD_MM_SELECTOR_VOL_STEP_2M_MAX` | `0.04` | `volatile`, "calm" score |
| `POLYBOT_REWARD_MM_SELECTOR_SWEEP_TARGET_LOTS` | `20.0` | "sweep" score |
| `POLYBOT_REWARD_MM_SELECTOR_SIZE_FIT_K` | `0.25` | min-shares affordability |
| `POLYBOT_REWARD_MM_SELECTOR_SPORTS_PRE_SECONDS` | `1800` | `live_sports` |
| `POLYBOT_REWARD_MM_SELECTOR_SPORTS_POST_SECONDS` | `14400` | `live_sports` |
| `POLYBOT_REWARD_MM_SCANNER_IMBALANCE_WARN` | `0.80` | `imbalanced` flag |
| `POLYBOT_REWARD_MM_SCANNER_SWEEP_FLOOR_MULT` | `1.0` | `sweepable` flag |

## Adverse-selection guards

The guards that decide when *not* to quote. See [STRATEGY.md](STRATEGY.md) for why each exists.

| Variable | Default | What it does |
| --- | --- | --- |
| `POLYBOT_REWARD_MM_TREND_DRIFT` | `0.06` | Pause entries when the midpoint drifts directionally by this much… |
| `POLYBOT_REWARD_MM_TREND_WINDOW_SECONDS` | `30` | …within this window. |
| `POLYBOT_REWARD_MM_GAP_GUARD_MOVE` | `0.04` | Pause on a sudden jump of this size… |
| `POLYBOT_REWARD_MM_GAP_GUARD_WINDOW_SECONDS` | `5` | …within this window… |
| `POLYBOT_REWARD_MM_GAP_GUARD_COOLDOWN_SECONDS` | `45` | …for this long. |
| `POLYBOT_REWARD_MM_POST_CLOSE_COOLDOWN_SECONDS` | `120` | Block re-entry after a stop. |
| `POLYBOT_REWARD_MM_GAME_PAUSE_ENABLED` | `true` | Pause entries around `game_start_time`… |
| `POLYBOT_REWARD_MM_GAME_PAUSE_PRE_SECONDS` | `900` | …this long before… |
| `POLYBOT_REWARD_MM_GAME_PAUSE_POST_SECONDS` | `14400` | …and after. |
| `POLYBOT_REWARD_MM_WARMUP_SECONDS` | `0.0` (off) | Skip a market for a while after first seeing it. |
| `POLYBOT_REWARD_MM_STABILITY_BAND` | `0.0` (off) | Pause when the raw midpoint deviates from its EMA. |
| `POLYBOT_REWARD_MM_MIDPOINT_EMA_ALPHA` | `0.3` | EMA smoothing for the band above. |

`WARMUP_SECONDS` and `STABILITY_BAND` are **off by default**: in testing they suppressed bundle
completion without measurably reducing fill-before-cancel. They are kept so the result can be
re-tested rather than re-derived.

## Post-fill and exits

| Variable | Default | What it does |
| --- | --- | --- |
| `POLYBOT_REWARD_MM_ARB_CLOSE_MAX_COST` | `1.010` | Cost ceiling for completing the YES+NO set. |
| `POLYBOT_REWARD_MM_SCRATCH_LOSS_PER_SHARE` | `0.01` | Passive scratch exit — works back to flat, not an emergency stop. |
| `POLYBOT_REWARD_MM_MAX_POSITION_SHARES` | `500` | Per-outcome inventory cap. |
| `POLYBOT_REWARD_MM_MAX_EXPOSURE_USDC` | `1000` | Per-market exposure cap (open inventory + working quotes). |
| `POLYBOT_REWARD_MM_MAX_NET_DELTA_SHARES` | `0.0` (off) | Cap aggregate unhedged exposure. Off: it was a portfolio-wide block where one position halted quoting everywhere. |
| `POLYBOT_REWARD_MM_RETIRE_MARKET_AFTER_FILL` | `true` | No new entries in a market that has taken a fill this run. |
| `POLYBOT_REWARD_MM_STOP_ACTION_THROTTLE_SECONDS` | `3.0` | Throttle stop retries while shares settle. |

## Reward estimation

Model coefficients behind the dashboard's `Est Reward`. Changing these changes reported numbers,
not bot behaviour.

`LIQUIDITY_SHARE_CAP_{CONSERVATIVE,BASE,OPTIMISTIC}` (`0.10` / `0.20` / `0.40`) cap the assumed
share of a pool. These were recalibrated against real payouts — an earlier 0.02 base cap
under-reported realised earnings roughly fourfold, so the cap now only bounds the degenerate
near-empty-book case.

Also: `COMPETITOR_FROM_BOOK` (`true` — measure rivals from the live book rather than assume),
`COMPETITOR_HAIRCUT_{CONSERVATIVE,BASE,OPTIMISTIC}`, `LIQUIDITY_COMPETITOR_SCORE_*`,
`MAKER_REBATE_RATE_*`, `ASSUMED_REWARD_SHARE`, `SINGLE_SIDED_SCORE_FACTOR`,
`SCANNER_COMPETITION_FLOOR`, `SCANNER_REWARD_REALISM_FACTOR`.

## Live execution

| Variable | Default | What it does |
| --- | --- | --- |
| `POLYBOT_REWARD_MM_LIVE_MAX_BALANCE_USDC` | `300` | Cap on total resting BUY notional. |
| `POLYBOT_REWARD_MM_MICRO_LIVE_MAX_MARKETS` | `1` | Hard cap on live markets. Raise deliberately. |
| `POLYBOT_REWARD_MM_LIVE_REQUIRE_DRY_RUN` | `true` | Require a wiring check within 24h. Do not turn this off. |
| `POLYBOT_REWARD_MM_LIVE_STOP_ON_UNTRACKED_FILL` | `true` | Halt on a fill the bot cannot attribute. |
| `POLYBOT_REWARD_MM_LIVE_RECONCILE_SECONDS` | `0.25` | Max wait between routine reconciles. Fills and adverse moves wake the loop immediately; this is only the fallback tick. |
| `POLYBOT_REWARD_MM_LIVE_MIN_RECONCILE_SECONDS` | `0.05` | Floor between reconciles, to stay inside the rate budget. |
| `POLYBOT_REWARD_MM_LIVE_BUY_CHASE_DELAY_SECONDS` | `30` | Minimum quote age before a BUY may be repriced upward. |
| `POLYBOT_REWARD_MM_LIVE_FEED_STALE_SECONDS` | `20` | Treat the feed as blind after this silence. |
| `POLYBOT_REWARD_MM_LIVE_BATCH_ORDERS` | `true` | Batch order placement. |

Live readiness (the dashboard's shadow gate): `LIVE_SHADOW_ENABLED`,
`LIVE_MIN_CONSERVATIVE_NET_USDC`, `LIVE_MAX_FILL_BEFORE_CANCEL_RATE` (`0.05`),
`LIVE_MAX_STOP_SLIPPAGE_USDC`, `LIVE_MAX_STALE_QUOTES`.

Auto-exit of small positions is **off** by default (`LIVE_AUTO_EXIT_MAX_SHARES=0`,
`LIVE_AUTO_EXIT_MAX_NOTIONAL_USDC=0`): a live stop cancels the market's orders and alerts rather
than selling at a loss. Set these to allow a bounded automatic taker exit.

## Paper simulation

Only affects `polybot reward-mm`; meaningless live.

`REWARD_MM_CANCEL_LATENCY_MS` (`300`) models how long a cancel takes, so trades that cross during
that window are counted as `Fill Before Cancel`. `SNAPSHOT_INTERVAL_SECONDS` (`5`) and
`SNAPSHOT_RETENTION_SECONDS` (`7200`) control dashboard sampling.

## Infrastructure

Rarely worth touching. Index build: `SELECTOR_POOL_LIMIT` (`20000`),
`SELECTOR_HYDRATE_PER_CATEGORY` (`60`), `SELECTOR_HYDRATE_LIMIT` (`600`),
`SELECTOR_VOL_CHECK_PER_CATEGORY` (`30`), `SELECTOR_VOL_CONCURRENCY` (`16`),
`SELECTOR_VOL_TIMEOUT_SECONDS` (`2.0`), `SELECTOR_POOL_CACHE_SECONDS` (`300`),
`SCANNER_METADATA_EVENT_LIMIT` (`6000` — the full, slow scan that populates the whole market
list; lowering it makes startup faster and the list shorter).

Connectivity: `GAMMA_HOST`, `CLOB_HOST`, `MARKET_WS_URL`, `USER_WS_URL`, `REQUEST_TIMEOUT_SECONDS`,
`MAX_RETRIES`, `WS_BATCH_SIZE`, `HEARTBEAT_SECONDS`, `LOG_LEVEL`,
`QUIET_CLOB_V2_REQUEST_ERRORS`.

Jurisdiction: `GEOBLOCK_URL`, `GEOBLOCK_FALLBACK_ENABLED`, `GEOBLOCK_IP_LOOKUP_URL`,
`GEOBLOCK_BLOCKED_COUNTRIES`. See the README before changing these.

Runtime paths: `KILL_SWITCH_FILE` (`.polybot-kill`), `LIVE_LOCK_FILE` (`.polybot-live.lock`),
`REWARD_MM_LIVE_DRY_RUN_MARKER` (`.polybot-dry-run`).

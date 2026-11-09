# Strategy

Polybot posts two-sided post-only quotes inside each Polymarket market's reward spread to earn
liquidity rewards, and manages the inventory risk that comes with being filled.

Everything below describes behaviour that is configurable; the environment variable controlling
each knob is named inline, and the full reference is [CONFIGURATION.md](CONFIGURATION.md).
Defaults live in `polybot/config.py` — you do not need to set any of them.

## How a run works

The engine discovers reward markets from the CLOB rewards API, plans post-only BUY quotes on both
outcome tokens inside each market's reward spread, tracks whether those quotes are scoring after
the configured minimum order age, and accounts for the inventory risk when they fill.

In paper mode (`polybot reward-mm`) quotes are virtual and fills are simulated. In live mode the
same planner's output is reconciled into real post-only orders. The engine is identical; only the
execution layer differs.

### Selecting the universe

Markets are **not** selected by raw `rate_per_day` — that ranking selects for low-competition
markets that other makers avoid for good reason. Instead the universe is filtered to drop:

- high recent volatility (`POLYBOT_REWARD_MM_MAX_ONE_DAY_CHANGE`),
- thin top-of-book depth (`POLYBOT_REWARD_MM_MIN_TOP_DEPTH_SHARES`),
- near-0/1 midpoints (`POLYBOT_REWARD_MM_MIN_MIDPOINT`),

then ranked by reward rate discounted by volatility.

`POLYBOT_REWARD_MM_SELECTION_MODE` (default `volume`) targets two-sided active markets — esports,
sports, popular binaries — where the YES+NO bundle income actually forms. Competition-aware
ranking (`projected`) was tried and tended to pick low-competition newsy markets; `rate`
reproduces the old `rate_per_day` ordering for comparison.

`POLYBOT_REWARD_MM_MAX_MIN_SIZE_SHARES` restricts the universe to cheap markets for a
small-bankroll test. `POLYBOT_REWARD_MM_EXCLUDE_KEYWORDS` (empty by default) blocks categories
manually, and `POLYBOT_REWARD_MM_MIN_HOURS_TO_RESOLUTION` (default 0) skips near-settlement
markets. Selection can be pinned to specific markets with `POLYBOT_REWARD_MM_MARKET_WHITELIST` —
which is what the web UI's market picker sets.

On each universe refresh, entry quotes for markets that dropped out of the selected set are
cancelled. Markets with open inventory stay watched as risk-only markets until the position is
closed, but they receive no fresh entry quotes.

### Placing quotes

Placement is driven by **depth ahead**, not by a fixed distance from the midpoint
(`POLYBOT_REWARD_MM_DEPTH_BASED_PLACEMENT`, on by default).

For a BUY, the shares "ahead" of a candidate tick are every bid priced above it **plus whatever
already rests at that tick** — joining a level means joining the back of its time-priority queue,
so a taker must consume that level before reaching you. Depth-ahead therefore grows as the tick
steps away from the midpoint: with bids of 29¢ ×100, 28¢ ×100 and 27¢ ×900, a buy at 29¢ has 100
shares ahead, at 28¢ has 200, and at 27¢ has 1,100.

The planner walks every tick from just under the midpoint (bounded by the post-only ceiling,
`best_ask - tick`) down to the reward-zone floor, keeps the ticks whose depth-ahead clears a
**floor buffer**, and among those picks the one nearest a **target buffer**. Because depth-ahead
only grows with distance, ties go to the shallowest adequately protected tick — staying as close to
the midpoint, and therefore as high in reward score, as is safe. Landing mid-band leaves room for
the book to move either way before the quote must be repriced.

The buffers scale with the market's reward `min_shares` — a proxy for taker lot size — through the
`DEPTH_TIER_MULTS` table in `polybot/rewards/planner.py`, not with your order size. When no tick in
the reward zone clears the floor, the book is too thin to farm and the market is not quoted.

In legacy mode (`DEPTH_BASED_PLACEMENT=false`) distance is instead a fixed fraction of the reward
spread, widened on volatile markets by `POLYBOT_REWARD_MM_DISTANCE_VOL_WIDEN_K`.

### Holding and repricing

An order placed mid-band is not repriced on every book tick. It **holds its price while its
depth-ahead stays inside `[floor, ceiling]`**, so ordinary churn in the levels in front of it —
the constant give-and-take of a live book — does not move the quote. Repricing burns two things:
API budget, and the moment of exposure between cancelling the old order and the new one resting.
A quote that only moves when it has to keeps both low.

The two band edges are **not symmetric**, and that asymmetry is what stops the quote oscillating:

- **Floor — a hard safety bound.** If depth-ahead falls below the floor, a taker could reach the
  order. The hold is released and the order is repriced deeper toward the target, no questions
  asked.
- **Ceiling — only a reward preference.** Sitting deeper than necessary costs a little reward
  score, not safety. So an upward (toward-midpoint) move is released *only* when the shallower tick
  the planner now prefers is itself robustly protected — its own depth-ahead clears the target. If
  that shallower tick is only marginally protected, moving up would just bounce the quote back a
  tick later, so the deeper, stable tick is held instead.

Without the asymmetry, a single large order straddling the band would make the quote flip between
two adjacent ticks indefinitely. A risk-driven size shrink and a fall out of the reward zone both
also release the hold.

The scoring midpoint and competitor liquidity both use a size-cutoff-adjusted midpoint
(`POLYBOT_REWARD_MM_DUST_SIZE_CUTOFF`, defaulting to each market's reward minimum size) so dust
orders don't distort placement.

New BUY quotes are capped by `POLYBOT_REWARD_MM_MAX_POSITION_SHARES` per outcome and
`POLYBOT_REWARD_MM_MAX_EXPOSURE_USDC` per market. Each market's open inventory plus working quotes
are bounded independently, so a fill on one market never suppresses quoting on the others.
Opposite-leg hedge quotes remain allowed even at the cap, so the bot can always reduce existing
risk.

### Estimating rewards honestly

The base `Est Reward` is computed from local reward Q-score, market reward rate, competitor
liquidity **measured from the live book inside the reward spread**, and a share cap — not from
"quote size × time". Estimates accrue on the snapshot cadence, so churned quotes are not
over-credited.

Results are tracked in conservative / base / optimistic scenarios. Maker fills also record
fee-equivalent and estimated maker-rebate scenarios when the market has fees enabled.

The dashboard's `Paying Reward` counts only markets whose projected daily reward clears
Polymarket's `POLYBOT_REWARD_MM_MIN_PAYOUT_USDC` ($1) floor — a day below it pays nothing and does
not roll over.

### Avoiding toxic fills

The central problem: a resting quote is an option written to anyone who knows more than you do.
Several guards address different versions of it.

**Trend / drift detector.** New entries pause when a market's midpoint drifts directionally by
`POLYBOT_REWARD_MM_TREND_DRIFT` over `POLYBOT_REWARD_MM_TREND_WINDOW_SECONDS` — a live match or a
news gap — while choppy, mean-reverting markets keep quoting. This is deliberately *not* a blanket
exclusion of volatile markets: esports and live sports are where the profitable two-sided bundle
income forms, so the cost is managed rather than avoided.

**Gap guard.** `POLYBOT_REWARD_MM_GAP_GUARD_MOVE` pauses new entry quotes on markets whose token
midpoints jump within `POLYBOT_REWARD_MM_GAP_GUARD_WINDOW_SECONDS`; the pause lasts
`POLYBOT_REWARD_MM_GAP_GUARD_COOLDOWN_SECONDS`.

**Game pause.** New entries pause inside a market's live-game window around `game_start_time`
(`POLYBOT_REWARD_MM_GAME_PAUSE_*`).

**Post-close cooldown.** A stop-loss close starts a
`POLYBOT_REWARD_MM_POST_CLOSE_COOLDOWN_SECONDS` re-entry cooldown, preventing churn back into a
market that just moved against the bot.

**Warmup and stability band** (both default **off**). A market can be skipped for
`POLYBOT_REWARD_MM_WARMUP_SECONDS` after first being seen, and entries can pause whenever the raw
midpoint deviates from its EMA by more than `POLYBOT_REWARD_MM_STABILITY_BAND` — which catches
live-match swings that `game_start_time` misses, since the reward API omits it for esports. They
are off by default because in testing they suppressed bundle completion without measurably
reducing fill-before-cancel. They are kept, and named, so the result can be re-tested rather than
re-derived.

**Fill-before-cancel accounting.** Replacements and cancels are simulated with
`POLYBOT_REWARD_MM_CANCEL_LATENCY_MS`; a trade that crosses during that window is tracked
separately as `Fill Before Cancel`. This is the metric that decides whether any of the guards
above are earning their keep.

### After a fill

A filled quote leaves an open position. There are three exits, tried in order:

1. **Complete the bundle.** Buy the complementary outcome for the same share count when the hedge
   is profitable, or reward-adjusted near flat. This locks PnL regardless of the market winner.
   `POLYBOT_REWARD_MM_ARB_CLOSE_AFTER_FILL` controls whether this runs automatically.
2. **Work a passive exit.** If no profitable hedge is available, post a passive SELL for the
   filled token while continuing to watch for a better hedge. The scratch SELL and the completing
   buy both keep earning rewards while the bot waits, so waiting is not free money lost. A
   scratch/trailing passive exit (`POLYBOT_REWARD_MM_SCRATCH_LOSS_PER_SHARE`) works losers back
   toward flat before the hard stop is ever reached.
3. **Emergency close.** Only when the worse of mark-to-market liquidation PnL and same-market
   hedge PnL reaches the run's max loss (`--max-loss-usdc`, the launch screen's "max loss /
   position"). The engine takes the less bad of selling the filled token or buying the opposite
   token, and records the trigger PnL, chosen route and stop slippage so gap losses can be
   audited afterwards.

There is exactly one stop-loss, and the operator sets it per run. Earlier per-share variants that
scaled or shadowed it were removed: a second stop that silently overrides the chosen number is a
footgun, not a safety feature.

In **live** mode a loss-stop cancels the market's orders and alerts — it never auto-sells at a
loss. Unwinding a real position is a decision left to the operator.

### Reconciling live orders

Live mode is a reconciliation layer: each pass diffs the planner's desired quotes against the
orders actually resting on the exchange, then cancels what no longer belongs and places what is
missing.

Both halves are **batched**. Polymarket's CLOB caps a single `POST /orders` or `DELETE /orders`
request at 15 orders — a larger batch is rejected outright — so the reconcile splits into
consecutive requests of 15 rather than falling back to one call per order. Managing orders
one-by-one costs more than latency: it multiplies requests against the rate budget, and every
extra millisecond between a book move and the cancel landing is fill risk.
`POLYBOT_REWARD_MM_LIVE_BATCH_ORDERS=false` reverts placement to the per-order path (cancels stay
batched either way, since that path is unconditionally safe).

The loop is event-driven rather than polled: the engine wakes it the moment it pulls a quote for
an adverse reason or books a fill, and those safety wakes bypass the inter-reconcile floor so an
endangered order is cancelled with minimal delay. `POLYBOT_REWARD_MM_LIVE_RECONCILE_SECONDS` is
only the fallback tick when nothing has happened.

### Live readiness gate

The dashboard exposes a shadow gate that must pass before live trading is considered ready:
conservative net PnL above the configured minimum, no stale quotes or state errors, emergency
exits and stop slippage within limits, and a fill-before-cancel rate below
`POLYBOT_REWARD_MM_LIVE_MAX_FILL_BEFORE_CANCEL_RATE`.

`reward-mm-live` additionally requires `POLYBOT_LIVE_ENABLED`,
`POLYBOT_REWARD_MM_MICRO_LIVE_ENABLED`, a configured wallet key, and a dry-run passed within 24
hours — the console refreshes that dry-run itself rather than letting a live run fall back to
paper. See [LIVE.md](LIVE.md).

---

## Dashboard

The built-in dashboard (and the Next.js UI in `web/`) surface:

- reward quotes: distance from midpoint vs max spread, scoring state, age;
- selected reward markets with rate/day, min size, tick, scoring/open quote counts, 24h volume;
- open inventory with average price, mark, risk PnL and distance to stop;
- daily PnL split between real reward payouts and trading PnL when live credentials are present;
- emergency exits, stop slippage and inventory state errors, called out as alerts;
- fill-before-cancel, stale-book and universe-refresh interruption counters.

## Safety

The live path requires an explicit live flag, environment-provided credentials, an inactive kill
switch, a recent dry-run, and a successful official geoblock check. It implements no geoblock
bypass. The only file that places orders is `polybot/execution/gateway.py`.

Never commit private keys. `.env` is gitignored — keep it that way.

Paper results are not a profit guarantee. Reward market making holds inventory, and inventory loses
money when markets move against it.

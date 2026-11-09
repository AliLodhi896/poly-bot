# Polybot

[![CI](https://github.com/vfeelv/polybot-rewards/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/vfeelv/polybot-rewards/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**A market-making bot that farms Polymarket's liquidity rewards program with the risk management that actually makes it survivable.**

![Polybot launch screen](docs/img/mainpage.png)

Polymarket pays a daily reward pool to makers whose resting orders sit inside a market's reward
spread. Collecting that is easy. *Keeping* it is not: your quotes get filled by people who know
something you don't, and a single bad fill on a market that gaps can erase a week of rewards.

Polybot posts two-sided post-only quotes inside the reward spread, places them by measured book
depth rather than a fixed offset, and spends some of its code on what happens **after** a fill.

```bash
uv sync --extra dev
cp .env.example .env
uv run polybot console
```

That opens a browser: pick markets, set limits, press start. It runs in **paper mode** by default -
simulated fills, no orders sent, no wallet needed.

---

## What's actually hard about this

Most reward-farming bots are a loop around `py-clob-client` that posts an order at
`midpoint ± max_spread` and hopes. That works until it doesn't. The parts of Polybot worth reading:

- **Orders are placed by the depth in front of them, not by a distance from the midpoint.**
  This is the core idea. For every candidate tick inside the reward zone the bot measures the
  *cumulative depth ahead*: each bid priced above that tick, **plus everything already resting at
  the tick itself** - joining a price level means joining the back of its time-priority queue, so
  those shares protect you too.

  Say the bids are 29¢ ×100, 28¢ ×100, 27¢ ×900. A buy at 29¢ has 100 shares ahead of it; at 28¢,
  200; at 27¢, 1,100. Needing a buffer near a thousand, the order goes to 27¢ - and it sits
  *behind* the 900 already there, not in front of them. A taker has to eat the entire buffer before
  it reaches you.

  The bot picks the **shallowest** tick that still clears its floor buffer, so it stays as close to
  the midpoint (highest reward score) as is safe. It aims not for a single number but for the
  *middle* of a `[floor, ceiling]` band of depth-ahead. The buffer scales with the market's reward
  `min_shares`, not with your order size. If no tick in the reward zone is protected, the bot does
  not quote that market at all.
- **A resting order holds its price until the depth in front of it actually leaves the band -
  it doesn't chase every tick.** Once placed mid-band, the order stays put while depth-ahead sits
  between the floor and the ceiling, so ordinary book noise doesn't move it. The two edges are
  deliberately **asymmetric**: the floor is a hard safety bound — if depth drops below it a taker
  could reach the order, so it reprices deeper immediately; the ceiling is only a reward
  preference, so moving *up* toward the midpoint happens only when the shallower tick is itself
  robustly protected. Without that asymmetry a single large order straddling the band would make
  the quote oscillate between two adjacent ticks forever. The result is a quote that sits still
  when it safely can and moves decisively when it must.
- **Resting orders are managed in batches of 15, the CLOB's own maximum.** A reconcile pass cancels
  and places in single batched requests rather than walking orders one at a time. Beyond 15 it
  splits into consecutive batches instead of degrading to per-order calls. One-by-one management
  isn't just slower — it multiplies requests against the API rate budget, and every extra
  millisecond between "the book moved" and "my order is gone" is fill risk.
- **Adverse selection is the whole game.** New quotes pause when a market's midpoint drifts
  directionally past a threshold inside a rolling window — a live match or a news gap — while
  choppy, mean-reverting markets keep quoting. Blanket-excluding volatile markets is the naive fix
  and it's wrong: esports and live sports are exactly where the two-sided bundle income forms.
- **Reward estimates from the real book, not from "size × time".** Competitor liquidity inside the
  reward spread is measured from the live order book, so the projected payout is your actual
  share of the pool. The dashboard is deliberately not flattering.
- **Fill-before-cancel is measured, not assumed.** Cancels are simulated with a configurable
  latency, and trades that cross during that window are tracked as their own metric — because
  that's the number that decides whether a parameter change helped.
- **Markets aren't ranked by `rate_per_day`.** That ranking selects for low-competition markets
  that everyone else avoids for good reason. The universe is filtered on volatility, top-of-book
  depth and distance from 0/1, then ranked with volatility discounted.
- **Post-fill management has three exits, in order:** complete the YES+NO bundle when the hedge is
  profitable or reward-adjusted near flat; otherwise work a passive scratch exit while still
  earning rewards; only a hard max-loss triggers an emergency taker close, and it records the
  trigger PnL, chosen route and slippage so gap losses can be audited afterwards.
- **Paper and live run the same engine.** The live path is a reconciliation layer over the same
  planner, so a paper run is a real rehearsal rather than a different program.

The full strategy write-up — universe selection, quote placement, reward scoring, every guard and
why it exists — is in **[docs/STRATEGY.md](docs/STRATEGY.md)**.

## The airdrop angle

Polymarket has **not announced a token or an airdrop**, and nothing here should be read as a claim
that one is coming. Treat this section as speculation about incentives, not a plan.

That said: if such a programme ever happens, the on-chain history it would most plausibly reward is
sustained, genuine market-making — real resting orders, real volume, real liquidity provided over
months, rather than a burst of wash activity before a snapshot. Running a reward bot produces
exactly that history as a by-product, and the liquidity rewards pay for the effort in the meantime.

The honest version of the argument: farm rewards because the rewards are real and measurable. If a
token ever follows, you were early for reasons that stand on their own. Sizing a position on the
hope of an unannounced airdrop is how people lose money.

## Quickstart

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and Node.js 20+ for the web UI.

Developed and tested on **macOS and Linux**. On **Windows**, run it inside
[WSL2](https://learn.microsoft.com/windows/wsl/install) (Ubuntu) — every command below works
unchanged there; native Windows is untested.

```bash
git clone https://github.com/vfeelv/polybot-rewards.git
cd polybot-rewards
uv sync --extra dev              # add --extra live when you intend to trade for real
cp .env.example .env
uv run polybot geoblock          # confirm this location may trade
uv run polybot console           # control plane + web UI on http://localhost:3000
```

The first launch builds the reward-market index (~2–3 minutes), then keeps it fresh.

Terminal-only, no web UI:

```bash
uv run polybot reward-mm --markets 5 --size 250 --max-loss-usdc 5 --port 8080
```

### Commands

| Command | What it does |
| --- | --- |
| `polybot console` | Control plane + web UI. Pick markets, set limits, start/stop. |
| `polybot reward-mm` | Paper reward market-making, terminal + built-in dashboard. |
| `polybot reward-mm-live` | The live path. Hard-gated — see below. |
| `polybot geoblock` | Report whether this location may trade. |

## Going live

Live trading is off behind three switches you have to set deliberately:

1. `POLYBOT_LIVE_ENABLED=true`
2. `POLYBOT_REWARD_MM_MICRO_LIVE_ENABLED=true`
3. `POLYBOT_WALLET_PRIVATE_KEY` set

Leave any of them unset and the bot runs the paper engine — that's the out-of-the-box experience,
and the UI says so plainly. Set all three and it trades for real.

The asymmetry is deliberate: **a configured live run never silently degrades to paper.** Before
going live it verifies the wiring with a dry-run — one tiny post-only order far from the midpoint,
placed and immediately cancelled, refreshed automatically when the 24h marker expires. If that
check fails, the start fails with the reason rather than quietly paper-trading.

Also: a kill switch (`touch .polybot-kill`) and a cross-process lock, so two live bots can never
quote the same wallet.

Full walkthrough — deposit wallet, collateral, approvals, dry-run, first real run:
**[docs/LIVE.md](docs/LIVE.md)**.

**Start with one market and the smallest size the market allows.** The paper engine models fills
optimistically in ways real queue position does not.

Live reconciliation has known rough edges — fill tracking can briefly disagree with your wallet,
and a just-placed order can lag in the dashboard. Read
**[docs/LIVE.md § Known rough edges](docs/LIVE.md#11-known-rough-edges--what-to-actually-watch)**
before your first live run: it says exactly what to watch and when.

## Configuration

Three layers, and most people never reach the third:

1. **[`.env`](.env.example)** — secrets, the live switches, and where the local server listens.
   About a dozen lines, and none of them need changing to run in paper mode.
2. **The launch screen** — what changes per run: markets, position size, max loss, duration,
   instant-arb-on-fill, halt-on-stop. There is exactly one stop-loss and you set it here.
3. **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** — strategy tuning: depth placement, market
   selection, the adverse-selection guards, reward-estimation coefficients, live execution.

Defaults live in `polybot/config.py` and nowhere else, so copying `.env.example` never silently
changes how the bot behaves.

## Development

```bash
uv run pytest
uv run ruff check .
cd web && npx tsc --noEmit && npx eslint src
```

Layout:

```
polybot/
  rewards/     reward-market selection, quote planning, scoring, live reconciliation
  execution/   the isolated live-order surface (gateway.py is the only file that places orders)
  feed/        CLOB market + user WebSockets, order book state
  api/         Gamma, CLOB REST, geoblock clients
  control/     process supervision + safety gates for UI-launched runs
  dashboard/   FastAPI snapshot/control API and the built-in HTML dashboard
web/           Next.js control UI (market browser, launch screen, live dashboard)
```

## Jurisdiction

Polymarket restricts trading in a number of jurisdictions. Polybot checks the official geoblock
endpoint before any live path can start and refuses to trade when blocked. That endpoint sits
behind Cloudflare and sometimes answers with a bot challenge; when it does, the bot falls back to
an independent IP-geolocation check against Polymarket's published restricted list, and treats an
undetermined country as blocked.

**Polybot implements no geoblock bypass, and requests to add one will be declined.** If you are in
a restricted jurisdiction, this software is not for you.

## Disclaimer

This software is provided as-is, for educational and research purposes. It places real orders with
real money when you configure it to.

- Nothing here is financial advice, and past or simulated results predict nothing.
- Liquidity rewards are not risk-free income. You are a market maker: you hold inventory, and
  inventory loses money when markets move against you.
- Paper results are optimistic. They cannot model queue position, and they model latency with an
  assumption rather than a measurement.
- Reward figures in the dashboard are *estimates* of your share of a pool that other participants
  are also competing for.
- You are responsible for your own compliance, taxes, and losses.

## Support

Polybot is free and MIT-licensed. If it earns you rewards — or you just want the write-ups on
what worked and what didn't in live runs - you can:

- Follow the build on X: [@hinntom](https://x.com/hinntom)
- New to Polymarket? [Sign up with my referral link](https://polymarket.com/?r=tomhinn) — it
  costs you nothing and supports development.

Stars help too, they are how other people find this.

## License

MIT — see [LICENSE](LICENSE).

## References

- [Polymarket API overview](https://docs.polymarket.com/api-reference)
- [Liquidity rewards](https://docs.polymarket.com/market-makers/liquidity-rewards)
- [Maker rebates](https://docs.polymarket.com/market-makers/maker-rebates)
- [Orders and post-only semantics](https://docs.polymarket.com/trading/orders/overview)
- [Market WebSocket](https://docs.polymarket.com/market-data/websocket/market-channel)

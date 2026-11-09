# Live setup — reward market making on real money

> **Paper stays the default.** Live trading requires all three of:
> `POLYBOT_LIVE_ENABLED=true`, `POLYBOT_REWARD_MM_MICRO_LIVE_ENABLED=true` and a configured
> `POLYBOT_WALLET_PRIVATE_KEY`. Leave any unset and the bot runs the paper engine instead — it
> does not error, it just doesn't trade. Set all three and it is live: a stale dry-run marker is
> re-verified rather than quietly demoting the run back to paper.
>
> Never commit `.env` or private keys (`.env` is gitignored — keep it that way).

This is the **deposit-wallet** flow. The original `py-clob-client` is archived; this uses
**`py-clob-client-v2`** with **signature type 3 (POLY_1271)** and **pUSD** collateral. The goal of
this guide is to get you to a **dry-run** — place and immediately cancel one tiny order. Real
farming is wired only after that passes.

> ⚠️ The programmatic deposit-wallet path is still early (e.g. py-clob-client-v2 issue #70 on
> sig-type-3 auth). If the dry-run hits an SDK bug, that is a signal to wait, not to push live.

## 1. Deposit wallet
A **deposit wallet** is a per-user ERC-1967 proxy. Your funds must live **in the deposit wallet**,
not in your EOA.
1. No Polymarket account yet? [Sign up here](https://polymarket.com/?r=tomhinn) *(referral link —
   costs you nothing, supports development)*.
2. Create/locate your deposit wallet (via the Polymarket app, or the relayer `WALLET-CREATE`
   flow). Note its **address** — this is your `funder`.
3. You still hold an **owner key** (EOA private key) that signs on behalf of the wallet.

## 2. Fund with pUSD (Polygon)
- Transfer **pUSD** (the CLOB collateral token) to the **deposit wallet address** (~$300 for the
  first test) plus a little **MATIC** for any on-chain steps.
- **pUSD held by your EOA does NOT count** as buying power for deposit-wallet orders — it must be
  in the deposit wallet itself.

## 3. Approvals + balance sync
- Approve the trading contracts for the deposit wallet (approvals must originate from the wallet,
  via the relayer `WALLET` batch, not the owner EOA). The Polymarket app does this when you first
  trade; otherwise use the v2 SDK / relayer.
- Balances are synced through the CLOB balance-allowance update with `signature_type=3`.

## 4. Install live dependencies
```bash
uv sync --extra live --extra dev    # installs py-clob-client-v2 (keep dev tools too)
```

## 5. Fill in `.env` (never commit it)
```
POLYBOT_WALLET_PRIVATE_KEY=0x...            # owner/signer key — secret
POLYBOT_WALLET_FUNDER_ADDRESS=0x...         # the DEPOSIT WALLET address (funder = maker = signer)
POLYBOT_WALLET_SIGNATURE_TYPE=3             # 3 = POLY_1271 (deposit wallet), the default
# CLOB API credentials are always derived from the key on start — there is nothing to set.

# Live gates (keep false until ready):
POLYBOT_LIVE_ENABLED=false
POLYBOT_REWARD_MM_MICRO_LIVE_ENABLED=false
POLYBOT_REWARD_MM_LIVE_MAX_BALANCE_USDC=300
```

## 6. Pre-flight checks
```bash
uv run polybot geoblock    # must show blocked=False for your location
```
If geoblocked, live trading refuses to start (by design). The bot implements no bypass.

## 7. Dry-run (one tiny order, immediately cancelled)
Enable both gates **only for this command**:
```bash
POLYBOT_LIVE_ENABLED=true POLYBOT_REWARD_MM_MICRO_LIVE_ENABLED=true \
  POLYBOT_REWARD_MM_MAX_MIN_SIZE_SHARES=40 \
  uv run polybot reward-mm-live --dry-run
```
It will: print your real pUSD balance → pick one cheap market → place a **post-only BUY ~20¢
below midpoint** (so it cannot fill) → confirm acceptance → **cancel it** → write a
`.polybot-dry-run` marker. Real risk ≈ a few cents for ~1 second.

If you see an SDK/API mismatch error, send it over — the live call surface is isolated in
`polybot/execution/gateway.py`, so fixes are one-file and quick.

## 8. Kill switch
Block all live placement immediately at any time:
```bash
touch .polybot-kill      # path = POLYBOT_KILL_SWITCH_FILE
```

## 9. Live farming — the first real test
After a passing dry-run (it writes `.polybot-dry-run`), start full live farming. The bot reconciles
the engine's desired quotes to **real post-only orders** (capped at `MAX_BALANCE_USDC`), takes
**real fills** from the user channel, and on a single-leg fill it first tries to **lock the bundle**
(buy the other leg with a price-capped FOK); on a loss-stop it **cancels and alerts you** to exit
manually (it never auto-sells at a loss). Start tiny, with the kill switch ready:
```bash
POLYBOT_LIVE_ENABLED=true POLYBOT_REWARD_MM_MICRO_LIVE_ENABLED=true \
  POLYBOT_REWARD_MM_MAX_MIN_SIZE_SHARES=40 \
  uv run polybot reward-mm-live --markets 1 --size 30 --port 8080
```
Watch the dashboard and your Polymarket account. Compare **actual reward payouts** to the
dashboard's estimate. Scale to 2–3 markets only after a stable single-market run. Kill switch:
`touch .polybot-kill`.

## 10. Running it from the web UI
Once `POLYBOT_LIVE_ENABLED`, `POLYBOT_REWARD_MM_MICRO_LIVE_ENABLED` and
`POLYBOT_WALLET_PRIVATE_KEY` are set, `polybot console` shows **"Live mode — real orders, real
funds"** on the launch screen instead of "Paper mode", and the start button launches
`reward-mm-live`. If it still says paper, the launch panel lists exactly which of the three is
missing.

The dry-run marker expires after 24 hours. A configured live run does **not** quietly become a
paper run when that happens: the console re-runs the wiring check itself (one tiny order, placed
and cancelled) and starts live. If that check fails — bad credentials, missing allowance, SDK
error — the start fails with the reason. It never silently trades on paper when you asked for
live.

## 11. Known rough edges — what to actually watch

Live reconciliation against a real exchange has failure modes paper cannot have. None of these
are theoretical; they are the ones observed in real runs, with the mitigations that exist today.

- **After any fill, verify the bot's view against the Polymarket UI.** The bot books fills from
  the user WebSocket channel; a dropped or duplicated message means its inventory can briefly
  disagree with your wallet. A periodic position sync (every
  `POLYBOT_REWARD_MM_LIVE_POSITION_SYNC_SECONDS`, default 60s) reads your real on-chain positions
  and **adopts** anything the bot under-tracks on a market it is trading — but it is adopt-only
  and the data API can lag a just-settled trade, so the minute after a fill is exactly when to
  glance at your balance and open positions in the Polymarket UI.
- **An open order can be slow to appear in the bot's dashboard.** The dashboard renders the
  engine's snapshot, not the exchange's; order state flows engine → SQLite → API → UI, so a
  just-placed or just-repriced order can lag a few seconds or, on a missed update, until the next
  reconcile/orphan sweep (`POLYBOT_REWARD_MM_LIVE_ORPHAN_SWEEP_SECONDS`, default 15s) catches it.
  The exchange's own UI is the source of truth for what is actually resting.
- **A fill for an order the bot does not recognise halts the run** (cancel-all + alert,
  `POLYBOT_REWARD_MM_LIVE_STOP_ON_UNTRACKED_FILL=true` by default). That state is too ambiguous
  for unattended real-money trading — treat the halt as a prompt to reconcile manually, not as a
  bug to disable.
- **A stopped position may need a manual exit.** Auto-exit is off by default
  (`POLYBOT_REWARD_MM_LIVE_AUTO_EXIT_MAX_SHARES=0`); on a stop-loss the bot cancels quotes and
  alerts, and shares from a very recent fill may not be settled yet, so the first exit attempt
  can fail with a balance error and retry on a throttle.

The practical routine: keep the Polymarket UI open next to the dashboard for the first sessions,
check both after every fill, and keep `touch .polybot-kill` one terminal away.

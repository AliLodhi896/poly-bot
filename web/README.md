# Polybot — Live Rewards Terminal

A standalone dark-themed trading dashboard for the live Polymarket liquidity-rewards
market-maker. It is a Next.js app that reads the bot's read-only `/api/snapshot`
endpoint and renders it as a real-time terminal. **It does not touch the bot's trading
logic** — it only visualizes the snapshot the engine already writes to SQLite.

Built with Next.js 16 (App Router), Tailwind v4, and [motion](https://motion.dev).

## How it connects

The live bot serves a FastAPI dashboard (HTML + `/api/snapshot`) on a port you pass to
`reward-mm-live`. This app proxies `/api/*` to that backend (see `next.config.ts`), so the
browser talks to a single origin — no CORS needed. The Python server also sends permissive
CORS headers, so you can point the app straight at the backend instead if you prefer.

Backend URL is configurable:

```bash
# defaults to http://127.0.0.1:8080
export POLYBOT_API_URL=http://127.0.0.1:8080
```

## Run it (one command)

From the repo root:

```bash
uv run polybot platform
```

This starts the control daemon (API on :8080) **and** the web UI (:3000), installs web deps
on first run, and opens the browser. When idle you get the **launch screen** (pick markets +
risk limits, then Start); while the bot runs you get the **dashboard** with a Stop button.
Press **Ctrl+C** once to stop everything. Flags: `--prod` (serve the built web app),
`--no-web` (daemon only), `--web-port`, `--port`, `--no-open-browser`.

### Or run the two pieces separately

```bash
uv run polybot control --port 8080   # control daemon + API (no bot until you press Start)
npm --prefix web run dev             # web UI on http://localhost:3000
```

Safety: the live gates (`POLYBOT_LIVE_ENABLED`, `POLYBOT_REWARD_MM_MICRO_LIVE_ENABLED`,
fresh dry-run) are enforced before a start; the UI shows blockers. A cross-process lock
(`.polybot-live.lock`) ensures only one live bot runs against the wallet — a second Start
(UI or terminal) is refused. Set `POLYBOT_CONTROL_TOKEN` to require a token on start/stop
when exposed beyond localhost.

The dashboard polls every 2.5s. If the daemon stops, the connection pill turns red and the
last snapshot stays on screen — it won't blank out.

## Production

```bash
npm run build
npm run start        # serves the built app + the /api proxy on :3000
```

## Real vs. estimated reward

The hero shows the **real** earned reward when the live bot supplies it. The dashboard
backend (`polybot/dashboard/rewards.py`, read-only) calls Polymarket's authenticated
`/rewards/user/total` (today's earned USDC) and `/rewards/user/percentages` (current
share → a real $/day run-rate). This only activates on the live path
(`reward-mm-live`, which passes `settings` into `create_app`). When unavailable
(paper, or no credentials) the hero falls back to the paper engine's
`estimated_reward_usdc` and shows a "paper model" badge. None of this touches the
trading loop.

## Layout / code map

- `src/lib/useSnapshot.ts` — polling hook, connection state, reward-history buffer.
- `src/lib/format.ts` — null-safe number/money/price/time formatters.
- `src/lib/types.ts` — snapshot shape (incl. `real_rewards`).
- `src/components/` — `StatusBar`, `Hero` (real reward + area chart), `Section`
  (borderless), `DataTable`, `EventsFeed`, `Value`, `AnimatedNumber`, `AreaChart`,
  `DottedSurface` (Three.js ambient wave).
- `src/app/page.tsx` — borderless editorial layout: hero, vitals strip, quotes +
  events, reward markets, dotted-surface footer.

Design is intentionally borderless (no card chrome), IBM Plex type, gold-on-midnight,
guided by the `ui-ux-pro-max` skill. Components are presentational and take plain props;
the only data dependency is the `/api/snapshot` JSON, so they drop into a larger platform.

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from polybot.config import Settings
from polybot.dashboard.rewards import RewardsReader
from polybot.storage import Storage

if TYPE_CHECKING:
    from polybot.control.manager import BotProcessManager
    from polybot.rewards.selector import SelectedMarket


def _market_to_dict(m: SelectedMarket) -> dict[str, object]:
    """Compact payload for the launch picker: a safety grade + reward + one risk line, with the
    raw signals kept under ``details`` for an optional expander."""
    return {
        "condition_id": m.condition_id,
        "question": m.question,
        "category": m.category,
        "grade": m.grade,
        "safety": m.safety,
        "risk_label": m.risk_label,
        "projected_reward": m.projected_reward,
        "rate_per_day": m.rate_per_day,
        "min_size": m.min_size,
        "required_size": m.required_size or m.min_size,
        "size_eligible": m.size_eligible,
        "mid": m.mid,
        "details": {
            "sweep_lots": m.sweep_lots,
            "volatility_10m": m.volatility_10m,
            "volatility_2m": m.volatility_2m,
            "vol_known": m.vol_known,
            "hours_left": m.hours_left,
            "one_day_change": m.one_day_change,
        },
    }


class StartBody(BaseModel):
    markets: int = 1
    size: float = 20.0
    max_loss_usdc: float = 0.5
    max_exposure_usdc: float | None = None
    max_position_shares: float | None = None
    seconds: float | None = None
    market_ids: list[str] = []
    halt_on_stop_loss: bool = False
    arb_close_after_fill: bool = True


def create_app(
    storage: Storage,
    settings: Settings | None = None,
    control: BotProcessManager | None = None,
) -> FastAPI:
    app = FastAPI(title="Polybot Reward MM", docs_url=None, redoc_url=None)
    # The snapshot + control API is consumed by the standalone web/ dashboard (and, later,
    # an external platform). Allow cross-origin requests so it works without a proxy too.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    # Only the live path passes settings; that enables the read-only real-rewards reader.
    rewards_reader = RewardsReader(settings) if settings else None
    token = settings.control_token if settings else None
    markets_cache: dict[tuple[int, float], tuple[float, dict[str, object]]] = {}
    markets_lock = asyncio.Lock()

    def _guard(authorization: str | None, x_control_token: str | None) -> None:
        if not token:
            return  # local single-operator default: control open on loopback
        supplied = x_control_token or (
            authorization.split(" ", 1)[1]
            if authorization and authorization.lower().startswith("bearer ")
            else None
        )
        if supplied != token:
            raise HTTPException(status_code=401, detail="invalid control token")

    @app.get("/", response_class=HTMLResponse)
    async def home() -> str:
        return _REWARD_HTML

    @app.get("/api/snapshot")
    async def snapshot() -> dict[str, object]:
        data = storage.reward_dashboard_snapshot(limit=40)
        if rewards_reader is not None:
            # SDK calls are synchronous + network-bound; keep the event loop free.
            data["real_rewards"] = await asyncio.to_thread(rewards_reader.snapshot, storage)
        return data

    if control is not None and settings is not None:

        @app.get("/api/control/status")
        async def control_status() -> dict[str, object]:
            return control.status()

        @app.post("/api/control/start")
        async def control_start(
            body: StartBody,
            authorization: str | None = Header(default=None),
            x_control_token: str | None = Header(default=None),
        ) -> dict[str, object]:
            from polybot.control.manager import GateError, LaunchParams

            _guard(authorization, x_control_token)
            try:
                return await asyncio.to_thread(control.start, LaunchParams(**body.model_dump()))
            except GateError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.post("/api/control/stop")
        async def control_stop(
            authorization: str | None = Header(default=None),
            x_control_token: str | None = Header(default=None),
        ) -> dict[str, object]:
            _guard(authorization, x_control_token)
            return await asyncio.to_thread(control.stop)

        @app.get("/api/markets/top")
        async def markets_top(limit: int = 10, size: float = 20.0) -> dict[str, object]:
            async with markets_lock:
                now = time.time()
                safe_size = max(1.0, float(size))
                cache_key = (int(limit), round(safe_size, 4))
                cached = markets_cache.get(cache_key)
                if cached is not None and now - cached[0] < 60:
                    return cached[1]
                from polybot.api.clob import ClobClient
                from polybot.rewards.selector import pool_status, select_by_category

                display_categories = ("weather", "finance", "sports", "politics", "tech", "other")
                try:
                    # Safety-first selector: hard gates exclude toxic markets, survivors ranked by
                    # reward. Read-only; never touches the trading path.
                    async with ClobClient(settings) as clob:
                        grouped = await select_by_category(
                            clob,
                            settings=settings,
                            per_category=limit,
                            size_shares=safe_size,
                        )
                    by_category: dict[str, list[dict[str, object]]] = {
                        cat: [_market_to_dict(m) for m in grouped.get(cat, [])]
                        for cat in display_categories
                    }
                    # The first full pool load runs in the background (~3 min); until it finishes
                    # the scan returns empty. Surface that as 'warming' (not an error) and do NOT
                    # cache the transient empty result, so the next poll picks up the pool as soon
                    # as it is ready instead of waiting out the 60s result cache.
                    status = pool_status(settings)
                    warming = status == "warming" and not any(by_category.values())
                    data: dict[str, object] = {
                        "by_category": by_category,
                        "warming": warming,
                        "error": (
                            "market scan unavailable"
                            if status == "error" and not any(by_category.values())
                            else None
                        ),
                    }
                    if warming:
                        return data
                except asyncio.CancelledError:
                    # Request cancelled mid-scan — almost always Ctrl+C on the control daemon
                    # (an in-flight scan outlives the 3s graceful-shutdown drain) or a client
                    # disconnect. The scanner is read-only, so there is nothing to roll back;
                    # return the last cached payload (or an empty one) rather than letting a
                    # CancelledError unwind into a noisy ASGI traceback. Deliberately not
                    # re-raised: this is a leaf GET handler and returning ends the request
                    # cleanly while the server is already shutting down.
                    return (
                        cached[1]
                        if cached is not None
                        else {
                            "by_category": {cat: [] for cat in display_categories},
                            "error": "scan cancelled",
                        }
                    )
                except Exception as exc:  # noqa: BLE001 — surface scan failures to the UI
                    data = {
                        "by_category": {cat: [] for cat in display_categories},
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                markets_cache[cache_key] = (now, data)
                return data

        @app.get("/api/markets")
        async def markets(
            page: int = 1,
            page_size: int = 24,
            search: str = "",
            category: str = "",
            grade: str = "",
            include_risky: bool = False,
            min_shares: float | None = None,
            max_shares: float | None = None,
            min_hours: float | None = None,
            sort: str = "reward",
            descending: bool = True,
        ) -> dict[str, object]:
            """Paginated, filterable, size-independent market browser over the shared index."""
            from polybot.rewards.index import markets_page

            return markets_page(
                settings,
                page=page,
                page_size=page_size,
                search=search,
                category=category,
                grade=grade,
                include_risky=include_risky,
                min_shares=min_shares,
                max_shares=max_shares,
                min_hours=min_hours,
                sort=sort,
                descending=descending,
            )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app


_REWARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polybot Reward MM Paper</title>
  <style>
    :root { --bg:#f5f7f8; --line:#d5dde3; --ink:#18212b; --muted:#657384;
      --good:#087d52; --bad:#bd372a; --warn:#a96012; --panel:#fff; }
    * { box-sizing:border-box; letter-spacing:0; }
    body { margin:0; background:var(--bg); color:var(--ink);
      font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif; }
    header { height:58px; background:var(--panel); border-bottom:1px solid var(--line);
      display:flex; align-items:center; justify-content:space-between; padding:0 22px; }
    h1 { font-size:18px; margin:0; font-weight:700; }
    #run { color:var(--muted); font-size:13px; }
    main { max-width:1540px; margin:16px auto; padding:0 16px; }
    .tiles { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px;
      margin-bottom:14px; }
    .tile, .panel { border:1px solid var(--line); background:var(--panel);
      border-radius:7px; }
    .tile { min-height:70px; padding:10px 12px; }
    .label { color:var(--muted); font-size:12px; margin-bottom:7px; }
    .num { font-size:21px; font-weight:650; white-space:nowrap; }
    .good { color:var(--good); } .bad { color:var(--bad); } .warn { color:var(--warn); }
    .columns { display:grid; grid-template-columns:1.1fr .9fr; gap:13px; }
    .panel { padding:13px; overflow:hidden; margin-bottom:13px; }
    h2 { font-size:15px; margin:0 0 11px; }
    table { border-collapse:collapse; width:100%; table-layout:fixed; }
    th { color:var(--muted); font-size:12px; text-align:left; font-weight:600; }
    td, th { border-bottom:1px solid #e8eef3; padding:8px 7px; overflow:hidden;
      white-space:nowrap; text-overflow:ellipsis; }
    td:last-child, th:last-child { text-align:right; }
    .empty { color:var(--muted); padding:22px 7px; }
    @media (max-width:1100px) { .tiles { grid-template-columns:repeat(3,1fr); }
      .columns { grid-template-columns:1fr; } }
    @media (max-width:560px) { header { padding:0 12px; } main { padding:0 9px; }
      .tiles { grid-template-columns:repeat(2,1fr); } .num { font-size:18px; } }
  </style>
</head>
<body>
  <header><h1>Polybot Reward MM Paper</h1><div id="run">waiting for run</div></header>
  <main>
    <section class="tiles" id="tiles"></section>
    <section class="columns">
      <div>
        <div class="panel"><h2>Open Reward Quotes</h2><div id="quotes"></div></div>
        <div class="panel"><h2>Reward Markets</h2><div id="markets"></div></div>
      </div>
      <div>
        <div class="panel"><h2>Would-Have Fills</h2><div id="fills"></div></div>
        <div class="panel"><h2>Open Unhedged Inventory</h2><div id="inventory"></div></div>
        <div class="panel"><h2>Matched Bundles</h2><div id="matched"></div></div>
        <div class="panel"><h2>State Errors</h2><div id="state-errors"></div></div>
        <div class="panel"><h2>Closed Inventory</h2><div id="closures"></div></div>
        <div class="panel"><h2>Events</h2><div id="events"></div></div>
      </div>
    </section>
  </main>
<script>
const htmlChars = {"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"};
const e = value => String(value ?? "").replace(/[&<>"']/g, c => htmlChars[c]);
const n = (value, digits=0) => value == null ? "-" : Number(value).toFixed(digits);
const money = value => value == null ? "-" : `$${Number(value).toFixed(3)}`;
const yn = value => Number(value || 0) ? "yes" : "-";
const pnlClass = value => Number(value || 0) >= 0 ? "good" : "bad";
const tiles = [
    ["Markets","markets_watched",0,""], ["Open Quotes","current_open_quotes",0,""],
    ["Selected Quotes","selected_open_quotes",0,""], ["Risk Quotes","risk_only_quotes",0,"warn"],
    ["Stale Quotes","stale_open_quotes",0,"bad"],
    ["Scoring Quotes","current_scoring_quotes",0,"good"],
    ["Non-Scoring","current_non_scoring_quotes",0,"warn"],
    ["Quote Seconds","scoring_quote_seconds",0,"good"],
 ["Est Reward","estimated_reward_usdc",3,"good"],
 ["Reward C","liquidity_reward_conservative_usdc",3,"good"],
 ["Reward B","liquidity_reward_base_usdc",3,"good"],
 ["Reward O","liquidity_reward_optimistic_usdc",3,"good"],
 ["Maker Fee Eq","maker_fee_equivalent_usdc",3,""],
 ["Maker Rebate B","maker_rebate_base_usdc",3,"good"],
 ["Would Fill","would_have_fills",0,"bad"],
 ["Fill Before Cancel","fill_before_cancel",0,"bad"],
 ["Cancel Pending","cancel_pending_quotes",0,"warn"],
 ["Cancel Acked","cancel_acknowledged_quotes",0,""],
 ["Trade Prints","trade_prints",0,""],
 ["Quote Opens","quotes_opened",0,""], ["Replacements","quote_replacements",0,"warn"],
 ["Snapshots","quote_snapshots",0,""], ["Score Weight","score_weight",0,""],
 ["Inventory Legs","inventory_positions",0,"warn"],
 ["Inventory Exposure","inventory_exposure",3,"warn"],
 ["Inventory PnL","inventory_unrealized_pnl",3,"pnl"],
 ["Locked Bundles","locked_bundles",0,"good"],
 ["Bundle PnL","locked_bundle_pnl",3,"pnl"],
 ["Passive Exits","passive_exits",0,"good"],
    ["Passive PnL","passive_exit_pnl",3,"pnl"],
    ["Emergency Exits","emergency_exits",0,"bad"],
    ["Emergency PnL","emergency_exit_pnl",3,"pnl"],
    ["Auto Cuts","auto_taker_exits",0,"warn"],
    ["Auto PnL","auto_taker_exit_pnl",3,"pnl"],
    ["Stop Slip","stop_slippage_usdc",3,"pnl"],
    ["Reward Net","reward_adjusted_net_pnl",3,"pnl"],
    ["Net C","reward_net_conservative_usdc",3,"pnl"],
    ["Net B","reward_net_base_usdc",3,"pnl"],
    ["Net O","reward_net_optimistic_usdc",3,"pnl"],
    ["Live Ready","live_readiness_passed",0,"good"],
    ["Live Blocks","live_readiness_blockers",0,"bad"],
    ["FBC Rate","live_readiness_fill_before_cancel_rate",3,"bad"],
    ["Max Loss","max_loss_usdc",3,"bad"],
    ["Max Position","max_position_shares",0,"warn"],
 ["Max Exposure","max_exposure_usdc",3,"warn"],
 ["Matched Bundles","matched_bundle_positions",0,"good"],
    ["Matched Shares","matched_bundle_shares",0,"good"],
    ["Matched PnL","matched_bundle_pnl",3,"pnl"],
    ["Unhedged Shares","unhedged_shares",0,"warn"],
    ["Unhedged PnL","unhedged_pnl",3,"pnl"],
    ["Cooldowns","cooldown_markets",0,"warn"],
    ["Game Paused","game_paused_markets",0,"warn"],
    ["Vol Reject","markets_rejected_volatility",0,"warn"],
    ["Depth Reject","markets_rejected_depth",0,"warn"],
    ["Extreme Reject","markets_rejected_extreme",0,"warn"],
    ["Res Reject","markets_rejected_resolution",0,"warn"],
    ["Cat Reject","markets_rejected_category",0,"warn"],
    ["Payout Reject","markets_rejected_low_payout",0,"warn"],
    ["Size Reject","markets_rejected_min_size",0,"warn"],
    ["Trending","trending_markets",0,"warn"],
    ["Unstable","unstable_markets",0,"warn"],
    ["Warmup Block","warmup_blocked_markets",0,"warn"],
    ["Paying Reward","markets_paying_reward",0,"good"],
    ["Competitor Q","competitor_q_avg",0,""],
    ["Snapshots Pruned","snapshots_pruned",0,""],
    ["State Errors","inventory_state_errors",0,"bad"],
    ["WS Groups","ws_connected",0,""], ["WS Messages","ws_messages",0,""],
    ["Refresh Errors","market_refresh_failures",0,"bad"]
];
function table(head, body) {
  if (!body.length) return '<div class="empty">No records yet</div>';
  return '<table><thead><tr>' + head.map(h => '<th>' + h + '</th>').join('')
    + '</tr></thead><tbody>' + body.join('') + '</tbody></table>';
}
let refreshInFlight = false;
async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const data = await fetch('/api/snapshot').then(r => r.json());
    const m = data.metrics || {}, run = data.run;
    document.getElementById('run').textContent = run
      ? `run #${run.id} | ${run.status} | paper only` : 'waiting for run';
    document.getElementById('tiles').innerHTML = tiles.map(([label,key,d,cls]) => {
      const value = ["estimated_reward_usdc","inventory_exposure","inventory_unrealized_pnl",
        "liquidity_reward_conservative_usdc","liquidity_reward_base_usdc",
        "liquidity_reward_optimistic_usdc","maker_fee_equivalent_usdc",
        "maker_rebate_conservative_usdc","maker_rebate_base_usdc",
        "maker_rebate_optimistic_usdc",
        "locked_bundle_pnl","passive_exit_pnl","emergency_exit_pnl",
        "stop_slippage_usdc","reward_adjusted_net_pnl",
        "reward_net_conservative_usdc","reward_net_base_usdc",
        "reward_net_optimistic_usdc","max_loss_usdc","max_exposure_usdc",
        "matched_bundle_pnl","unhedged_pnl"]
        .includes(key) ? money(m[key]) : n(m[key],d);
      const color = cls === "pnl" ? pnlClass(m[key]) : cls;
      return `<div class="tile"><div class="label">${label}</div>`
        + `<div class="num ${color}">${value}</div></div>`;
    }).join('');
    document.getElementById('quotes').innerHTML = table(
      ['Market','Outcome','Quote','State','Mid','Dist / Max','Age','Scoring','Reason'],
      (data.quotes || []).map(x => `<tr><td title="${e(x.question)}">${e(x.question)}</td>`
        + `<td>${e(x.outcome)}</td><td>${e(x.side)} ${n(x.price,4)} x ${n(x.size,0)}</td>`
        + `<td>${e(x.lifecycle_state || x.status)}</td>`
        + `<td>${n(x.midpoint,4)}</td>`
        + `<td>${n(x.distance_from_midpoint,4)} / ${n(x.max_spread,4)}</td>`
        + `<td>${n(x.age_seconds,0)}s</td>`
        + `<td class="${Number(x.locally_scoring) ? 'good' : 'warn'}">${yn(x.locally_scoring)}</td>`
        + `<td>${e(x.reason || '')}</td></tr>`)
    );
    document.getElementById('markets').innerHTML = table(
      ['Market','Rate/Day','Min Size','Max Spread','Tick','Quotes','Volume'],
      (data.markets || []).map(x => `<tr><td title="${e(x.question)}">${e(x.question)}</td>`
        + `<td>${money(x.rewards_rate_per_day)}</td><td>${n(x.rewards_min_size,0)}</td>`
        + `<td>${n(x.rewards_max_spread,4)}</td><td>${n(x.tick_size,4)}</td>`
        + `<td>${n(x.scoring_quotes,0)} / ${n(x.open_quotes,0)}</td>`
        + `<td>${n(x.volume_24hr,0)}</td></tr>`)
    );
    document.getElementById('fills').innerHTML = table(
      ['Market','Outcome','State','Trade','Quote','Size','Rebate B','Time'],
      (data.fills || []).map(x => `<tr><td title="${e(x.question)}">${e(x.question)}</td>`
        + `<td>${e(x.outcome)}</td><td>${e(x.lifecycle_state)}</td>`
        + `<td>${n(x.trade_price,4)}</td>`
        + `<td>${n(x.quote_price,4)}</td><td>${n(x.size,0)}</td>`
        + `<td>${money(x.maker_rebate_base_usdc)}</td>`
        + `<td>${new Date(Number(x.local_ts || 0)*1000).toLocaleTimeString()}</td></tr>`)
    );
    document.getElementById('inventory').innerHTML = table(
      ['Market','State','Outcome','Shares','Unhedged','Avg','Bid',
       'Liq PnL','Risk PnL','To Stop','Hedge','Exit'],
      (data.inventory || []).map(x => `<tr><td title="${e(x.question)}">${e(x.question)}</td>`
        + `<td>${e(x.state)}</td><td>${e(x.outcome)}</td><td>${n(x.shares,2)}</td>`
        + `<td>${n(x.unhedged_shares,2)}</td>`
        + `<td>${n(x.avg_price,4)}</td><td>${n(x.mark_price,4)}</td>`
        + `<td class="${pnlClass(x.liquidation_pnl)}">${money(x.liquidation_pnl)}</td>`
        + `<td class="${pnlClass(x.risk_pnl)}">${money(x.risk_pnl)}</td>`
        + `<td class="${pnlClass(x.loss_to_stop)}">${money(x.loss_to_stop)}</td>`
        + `<td>${n(x.best_hedge_bundle_cost,4)} / ${money(x.best_hedge_pnl)}</td>`
        + `<td>${n(x.passive_exit_price,4)}</td></tr>`)
    );
    document.getElementById('matched').innerHTML = table(
      ['Market','Outcomes','Shares','Bundle','Matched PnL'],
      (data.matched_bundles || []).map(x => `<tr><td title="${e(x.question)}">${e(x.question)}</td>`
        + `<td>${e(x.outcome_a)} / ${e(x.outcome_b)}</td><td>${n(x.matched_shares,2)}</td>`
        + `<td>${n(x.bundle_cost,4)}</td>`
        + `<td class="${pnlClass(x.matched_pnl)}">${money(x.matched_pnl)}</td></tr>`)
    );
    document.getElementById('state-errors').innerHTML = table(
      ['Market','Outcome','State','Shares','Closed','Updated'],
      (data.state_errors || []).map(x => `<tr><td title="${e(x.question)}">${e(x.question)}</td>`
        + `<td>${e(x.outcome)}</td><td>${e(x.state)}</td><td>${n(x.shares,2)}</td>`
        + `<td>${new Date(Number(x.closed_at || 0)*1000).toLocaleTimeString()}</td>`
        + `<td>${new Date(Number(x.updated_at || 0)*1000).toLocaleTimeString()}</td></tr>`)
    );
    document.getElementById('closures').innerHTML = table(
      ['Market','Mode','Route','Reason','Outcome','Shares','Entry',
       'Exit/Hedge','PnL','Trigger','Slip','Reward Net'],
      (data.closures || []).map(x => `<tr><td title="${e(x.question)}">${e(x.question)}</td>`
        + `<td>${e(x.close_mode)}</td><td>${e(x.exit_route)}</td>`
        + `<td>${e(x.risk_reason)}</td><td>${e(x.outcome)}</td><td>${n(x.shares,2)}</td>`
        + `<td>${n(x.entry_price,4)}</td>`
        + `<td>${n(x.exit_price ?? x.hedge_price,4)}</td>`
        + `<td class="${pnlClass(x.realized_pnl)}">${money(x.realized_pnl)}</td>`
        + `<td class="${pnlClass(x.trigger_pnl)}">${money(x.trigger_pnl)}</td>`
        + `<td class="${pnlClass(x.stop_slippage_usdc)}">${money(x.stop_slippage_usdc)}</td>`
        + `<td class="${pnlClass(x.reward_adjusted_pnl)}">`
        + `${money(x.reward_adjusted_pnl)}</td></tr>`)
    );
    document.getElementById('events').innerHTML = table(
      ['Type','Message'],
      (data.events || []).map(x => `<tr><td>${e(x.event_type)}</td>`
        + `<td title="${e(x.message)}">${e(x.message)}</td></tr>`)
    );
  } catch (_) { document.getElementById('run').textContent = 'snapshot unavailable'; }
  finally { refreshInFlight = false; }
}
refresh(); setInterval(refresh, 2500);
</script>
</body></html>"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import UTC, datetime

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from polybot.api.clob import ClobClient
from polybot.api.geoblock import GeoblockClient
from polybot.config import Settings, get_settings
from polybot.dashboard.server import create_app
from polybot.rewards.engine import RewardMMEngine
from polybot.storage import Storage

app = typer.Typer(
    add_completion=False,
    help="Polymarket liquidity-rewards market-making bot. Paper by default; live is gated.",
    no_args_is_help=True,
)
console = Console()


def settings_and_storage() -> tuple[Settings, Storage]:
    settings = get_settings()
    storage = Storage(settings.database_path)
    storage.init()
    return settings, storage


def run_async(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


@app.command("reward-mm")
def reward_mm_run(
    markets: int = typer.Option(50, min=1, help="Maximum reward markets to observe."),
    size: float | None = typer.Option(
        None,
        min=0.01,
        help="Shares per quote; defaults to POLYBOT_REWARD_MM_SIZE_SHARES.",
    ),
    max_loss_usdc: float | None = typer.Option(
        None,
        min=0.001,
        help="Max mark-to-market loss per open inventory position.",
    ),
    max_position_shares: float | None = typer.Option(
        None,
        min=0.01,
        help=(
            "Max open shares per market outcome; defaults to "
            "POLYBOT_REWARD_MM_MAX_POSITION_SHARES."
        ),
    ),
    max_exposure_usdc: float | None = typer.Option(
        None,
        min=0.01,
        help="Max total open inventory cost; defaults to POLYBOT_REWARD_MM_MAX_EXPOSURE_USDC.",
    ),
    host: str | None = typer.Option(None, help="Dashboard bind host."),
    port: int | None = typer.Option(None, help="Dashboard bind port."),
    seconds: float | None = typer.Option(None, min=0.1, help="Stop after N seconds."),
    no_dashboard: bool = typer.Option(
        False,
        "--no-dashboard",
        help="Skip the built-in dashboard server (the control daemon owns the port).",
    ),
) -> None:
    """Run paper-only Liquidity Rewards market-making research."""
    settings, storage = settings_and_storage()
    if settings.live_enabled:
        console.print(
            "[yellow]POLYBOT_LIVE_ENABLED is ignored: `reward-mm` is paper-only.[/yellow]"
        )
    effective_size = size if size is not None else settings.reward_mm_size_shares
    effective_max_loss = (
        max_loss_usdc if max_loss_usdc is not None else settings.reward_mm_max_loss_usdc
    )
    effective_max_position = (
        max_position_shares
        if max_position_shares is not None
        else settings.reward_mm_max_position_shares
    )
    effective_max_exposure = (
        max_exposure_usdc
        if max_exposure_usdc is not None
        else settings.reward_mm_max_exposure_usdc
    )
    run_async(
        _serve_and_collect_reward(
            settings,
            storage,
            markets=min(markets, settings.reward_mm_market_limit),
            size=effective_size,
            max_loss_usdc=effective_max_loss,
            max_position_shares=effective_max_position,
            max_exposure_usdc=effective_max_exposure,
            host=host or settings.dashboard_host,
            port=port or settings.dashboard_port,
            seconds=seconds,
            no_dashboard=no_dashboard,
        )
    )


@app.command("reward-mm-live")
def reward_mm_live_shadow(
    markets: int = typer.Option(1, min=1, help="Maximum reward markets for the live scaffold."),
    size: float | None = typer.Option(
        None,
        min=0.01,
        help="Shares per quote; defaults to POLYBOT_REWARD_MM_SIZE_SHARES.",
    ),
    max_loss_usdc: float | None = typer.Option(
        None,
        min=0.001,
        help="Max mark-to-market loss per open inventory position.",
    ),
    host: str | None = typer.Option(None, help="Dashboard bind host."),
    port: int | None = typer.Option(None, help="Dashboard bind port."),
    seconds: float | None = typer.Option(None, min=0.1, help="Stop after N seconds."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Place and immediately cancel one tiny far-from-mid order to verify live wiring.",
    ),
    no_dashboard: bool = typer.Option(
        False,
        "--no-dashboard",
        help="Run the bot without its own dashboard server (the control daemon owns the port).",
    ),
) -> None:
    """Run reward market making against real money. Hard-gated — see docs/LIVE.md.

    PLACES REAL ORDERS once every gate passes: POLYBOT_LIVE_ENABLED,
    POLYBOT_REWARD_MM_MICRO_LIVE_ENABLED, a dry-run marker from the last 24h, an inactive kill
    switch, and no other live bot holding the single-instance lock. A missing gate refuses the
    start rather than quietly trading.
    """
    settings, storage = settings_and_storage()
    if dry_run:
        if not (settings.live_enabled and settings.reward_mm_micro_live_enabled):
            console.print(
                "[red]Dry-run requires POLYBOT_LIVE_ENABLED=true and "
                "POLYBOT_REWARD_MM_MICRO_LIVE_ENABLED=true.[/red]"
            )
            raise typer.Exit(1)
        run_async(_reward_mm_dry_run(settings))
        return
    if not (settings.live_enabled and settings.reward_mm_micro_live_enabled):
        console.print(
            "[red]Live farming requires POLYBOT_LIVE_ENABLED=true and "
            "POLYBOT_REWARD_MM_MICRO_LIVE_ENABLED=true.[/red]"
        )
        raise typer.Exit(1)
    if settings.reward_mm_live_require_dry_run and not _dry_run_marker_fresh(settings):
        console.print(
            "[red]No recent dry-run. Run `polybot reward-mm-live --dry-run` first "
            "(it writes the .polybot-dry-run marker).[/red]"
        )
        raise typer.Exit(1)
    effective_size = size if size is not None else settings.reward_mm_size_shares
    effective_max_loss = (
        max_loss_usdc if max_loss_usdc is not None else settings.reward_mm_max_loss_usdc
    )
    effective_markets = min(
        markets,
        settings.reward_mm_market_limit,
        settings.reward_mm_micro_live_max_markets,
    )
    # Single, user-facing exposure knob: POLYBOT_REWARD_MM_MAX_EXPOSURE_USDC (applied PER-MARKET
    # in the engine). The micro-live exposure sub-cap is deliberately NOT min()'d in here — it used
    # to silently override the configured value. Live scope is still bounded by
    # micro_live_max_markets and reward_mm_max_position_shares.
    effective_max_exposure = settings.reward_mm_max_exposure_usdc
    console.print(
        "[bold red]LIVE FARMING: this submits REAL post-only orders with REAL money. "
        "Keep the kill switch (.polybot-kill) ready.[/bold red]"
    )
    from polybot.control.lock import SingleInstanceLock, held_by

    lock = SingleInstanceLock(settings.live_lock_file)
    if not lock.acquire():
        pid = held_by(settings.live_lock_file)
        suffix = f" (pid {pid})" if pid and pid > 0 else ""
        console.print(
            f"[red]Another live bot is already running{suffix}. "
            f"Refusing to start a second instance on the same wallet.[/red]"
        )
        raise typer.Exit(1)
    try:
        run_async(
            _serve_and_collect_live(
                settings,
                storage,
                markets=effective_markets,
                size=effective_size,
                max_loss_usdc=effective_max_loss,
                max_position_shares=settings.reward_mm_max_position_shares,
                max_exposure_usdc=effective_max_exposure,
                host=host or settings.dashboard_host,
                port=port or settings.dashboard_port,
                seconds=seconds,
                no_dashboard=no_dashboard,
            )
        )
    finally:
        lock.release()


@app.command("control")
def control_daemon(
    host: str | None = typer.Option(None, help="Control/dashboard bind host."),
    port: int | None = typer.Option(None, help="Control/dashboard bind port."),
) -> None:
    """Run the always-on control plane (serves the dashboard + start/stop the live bot)."""
    settings, storage = settings_and_storage()
    from polybot.control.manager import BotProcessManager

    manager = BotProcessManager(settings)
    bind_host = host or settings.dashboard_host
    bind_port = port or settings.dashboard_port
    console.print(
        f"Control plane: [link=http://{bind_host}:{bind_port}]"
        f"http://{bind_host}:{bind_port}[/link]  (Ctrl+C also stops the bot)"
    )
    _serve_control(
        create_app(storage, settings=settings, control=manager),
        manager,
        host=bind_host,
        port=bind_port,
        log_level=settings.log_level.lower(),
    )


@app.command("console")
def console_cmd(
    port: int | None = typer.Option(None, help="Control/dashboard API port."),
    web_port: int = typer.Option(3000, help="Web UI port."),
    no_web: bool = typer.Option(False, "--no-web", help="Run only the control daemon."),
    prod: bool = typer.Option(
        False, "--prod", help="Serve the built web app (npm run start) instead of dev."
    ),
    open_browser: bool = typer.Option(True, help="Open the web UI in a browser on start."),
) -> None:
    """One command: launch the control daemon + web UI. Press Start in the browser.

    Runs the paper engine by default. Real orders require the live gates (see README).
    """
    import os
    import shutil
    import signal
    import subprocess
    import threading
    import webbrowser
    from pathlib import Path

    settings, storage = settings_and_storage()
    from polybot.control.manager import BotProcessManager

    bind_host = settings.dashboard_host
    bind_port = port or settings.dashboard_port
    web_proc: subprocess.Popen[bytes] | None = None

    if not no_web:
        web_dir = Path(__file__).resolve().parents[1] / "web"
        npm = shutil.which("npm")
        if npm is None:
            console.print("[red]npm not found. Install Node.js, or run with --no-web.[/red]")
            raise typer.Exit(1)
        if not (web_dir / "node_modules").exists():
            console.print("Installing web dependencies (first run only)…")
            # `npm ci` installs exactly what the committed lockfile says and never rewrites it
            # (a bare `npm install` may re-resolve and dirty package-lock.json on first launch).
            installer = "ci" if (web_dir / "package-lock.json").exists() else "install"
            subprocess.run([npm, installer], cwd=str(web_dir), check=True)  # noqa: S603
        env = dict(os.environ)
        env["POLYBOT_API_URL"] = f"http://127.0.0.1:{bind_port}"
        env["PORT"] = str(web_port)
        web_proc = subprocess.Popen(  # noqa: S603
            [npm, "run", "start" if prod else "dev"],
            cwd=str(web_dir),
            env=env,
            start_new_session=True,
        )
        console.print(f"Web UI: [link=http://localhost:{web_port}]http://localhost:{web_port}[/link]")
        if open_browser:
            threading.Timer(4.0, lambda: webbrowser.open(f"http://localhost:{web_port}")).start()

    console.print(
        f"Control plane: [link=http://{bind_host}:{bind_port}]"
        f"http://{bind_host}:{bind_port}[/link]  (Ctrl+C stops everything)"
    )
    manager = BotProcessManager(settings)

    def _kill_web() -> None:
        if web_proc is not None:
            os.killpg(os.getpgid(web_proc.pid), signal.SIGTERM)

    _serve_control(
        create_app(storage, settings=settings, control=manager),
        manager,
        host=bind_host,
        port=bind_port,
        log_level=settings.log_level.lower(),
        after_shutdown=_kill_web,
    )


def _shutdown_bot(manager: object) -> None:
    """Gracefully stop the live bot on daemon shutdown (cancels all resting orders)."""
    import contextlib

    status = getattr(manager, "status", None)
    if status is None or not status().get("running"):
        return
    console.print("[yellow]Stopping live bot (cancelling resting orders)…[/yellow]")
    with contextlib.suppress(Exception):
        manager.stop()  # type: ignore[attr-defined]
    console.print("[green]Live bot stopped.[/green]")


def _serve_control(
    app: object,
    manager: object,
    *,
    host: str,
    port: int,
    log_level: str,
    after_shutdown: object | None = None,
) -> None:
    """Serve the control plane, stopping the live bot FIRST on Ctrl+C.

    uvicorn runs in a background thread with its own signal handlers disabled; the main
    thread owns SIGINT/SIGTERM. On a stop signal we cancel the bot's resting orders before
    unwinding the server, so a slow or stuck server can never strand real open orders.
    Must be called from the main thread (signal handlers require it).
    """
    import contextlib
    import signal
    import threading

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=log_level,
            access_log=False,
            # Bound the drain so a lingering keep-alive (e.g. the web UI's pollers) can't
            # stall "Waiting for connections to close" — force-close after a few seconds.
            timeout_graceful_shutdown=3,
        )
    )
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    stop_event = threading.Event()
    prev_int = signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    prev_term = signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    thread = threading.Thread(target=server.run, name="control-uvicorn", daemon=True)
    thread.start()
    try:
        while not stop_event.is_set() and thread.is_alive():
            stop_event.wait(0.25)
    finally:
        with contextlib.suppress(Exception):
            signal.signal(signal.SIGINT, prev_int)
            signal.signal(signal.SIGTERM, prev_term)
        _shutdown_bot(manager)  # 1) cancel live orders first, whatever the server is doing
        if after_shutdown is not None:  # 2) stop the web UI so it stops polling 8080 (cuts
            with contextlib.suppress(Exception):  #    the ECONNREFUSED proxy-error spam)
                after_shutdown()  # type: ignore[operator]
        server.should_exit = True  # 3) then unwind the control server
        thread.join(timeout=10)


async def _serve_and_collect_reward(
    settings: Settings,
    storage: Storage,
    *,
    markets: int,
    size: float,
    max_loss_usdc: float,
    max_position_shares: float,
    max_exposure_usdc: float,
    host: str,
    port: int,
    seconds: float | None,
    no_dashboard: bool = False,
) -> None:
    async def _run() -> dict[str, object]:
        return await RewardMMEngine(settings, storage).run(
            markets,
            size_shares=size,
            max_loss_usdc=max_loss_usdc,
            max_position_shares=max_position_shares,
            max_exposure_usdc=max_exposure_usdc,
            seconds=seconds,
        )

    console.print(
        f"Paper only: reward markets<={markets}, quote size={size:g} shares, "
        f"max_loss=${max_loss_usdc:.2f}, max_position={max_position_shares:g}, "
        f"max_exposure=${max_exposure_usdc:.2f}"
    )
    if no_dashboard:
        # The control daemon owns the dashboard port; just run the trading loop.
        await _run()
        return

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(storage),
            host=host,
            port=port,
            log_level=settings.log_level.lower(),
            access_log=False,
        )
    )
    server_task = asyncio.create_task(server.serve())
    console.print(f"Dashboard: [link=http://{host}:{port}]http://{host}:{port}[/link]")
    try:
        metrics = await _run()
        if seconds is not None:
            console.print(
                f"Stopped: markets={metrics['markets_watched']}, "
                f"scoring_quotes={metrics['current_scoring_quotes']}, "
                f"would_fill={metrics['would_have_fills']}"
            )
    finally:
        server.should_exit = True
        await server_task


def _dry_run_marker_fresh(settings: Settings, max_age_hours: float = 24.0) -> bool:
    marker = settings.reward_mm_live_dry_run_marker
    if not marker.exists():
        return False
    return (time.time() - marker.stat().st_mtime) <= max_age_hours * 3600


async def _serve_and_collect_live(
    settings: Settings,
    storage: Storage,
    *,
    markets: int,
    size: float,
    max_loss_usdc: float,
    max_position_shares: float,
    max_exposure_usdc: float,
    host: str,
    port: int,
    seconds: float | None,
    no_dashboard: bool = False,
) -> None:
    from polybot.execution.gateway import LiveOrderGateway
    from polybot.rewards.live import LiveRewardCoordinator

    engine = RewardMMEngine(settings, storage)
    gateway = LiveOrderGateway(settings)
    coordinator = LiveRewardCoordinator(settings, storage, engine, gateway)

    async def _run() -> None:
        await coordinator.run(
            markets,
            size_shares=size,
            max_loss_usdc=max_loss_usdc,
            max_position_shares=max_position_shares,
            max_exposure_usdc=max_exposure_usdc,
            seconds=seconds,
        )

    if no_dashboard:
        # The control daemon owns the dashboard port; just run the trading loop.
        await _run()
        return

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(storage, settings=settings),
            host=host,
            port=port,
            log_level=settings.log_level.lower(),
            access_log=False,
        )
    )
    server_task = asyncio.create_task(server.serve())
    console.print(f"Dashboard: [link=http://{host}:{port}]http://{host}:{port}[/link]")
    try:
        await _run()
    finally:
        server.should_exit = True
        await server_task


# Tick sizes the py-clob-client order builder can round to (keys of its ROUNDING_CONFIG).
_SUPPORTED_TICKS = frozenset({"0.1", "0.01", "0.001", "0.0001"})


async def _reward_mm_dry_run(settings: Settings) -> None:
    """Verify live connectivity: read balance, place one far-from-mid post-only order, cancel it."""
    from polybot.execution.gateway import LiveOrderGateway

    gateway = LiveOrderGateway(settings)
    await gateway.start()  # enforces LIVE_ENABLED, kill switch, geoblock, credentials
    console.print(f"[green]Live connection OK. USDC balance: ${gateway.usdc_balance():.2f}[/green]")

    cap = settings.reward_mm_max_min_size_shares
    async with ClobClient(settings) as clob:
        raw_markets, _cursor = await clob.get_rewards_markets(
            limit=80, order_by="rate_per_day", position="DESC"
        )
        choice = None
        for market in raw_markets:
            condition_id = getattr(market, "condition_id", "")
            rewards = getattr(market, "rewards", None)
            tokens = getattr(market, "tokens", [])
            if not condition_id or rewards is None or len(tokens) < 2:
                continue
            min_size = rewards.min_size or 5.0
            if cap > 0 and min_size > cap:
                continue
            info = await clob.get_clob_market_info(condition_id)
            tick = float(info.tick_size)
            # The CLOB order builder only understands the standard ticks; a market that
            # reports anything else (e.g. 0.0025) would KeyError inside create_order, so
            # skip it and keep scanning rather than fail the whole dry-run.
            if str(tick) not in _SUPPORTED_TICKS:
                continue
            token_id = tokens[0].token_id
            book = await clob.get_order_book(token_id)
            if book.midpoint is None:
                continue
            # Smallest possible resting BUY: 1 cent (or one tick if the tick is coarser), far
            # below any mid so it rests post-only and never fills — minimal capital at risk.
            price = max(tick, math.floor(round(0.01 / tick, 6)) * tick)
            if price <= 0 or price >= float(book.midpoint):
                continue
            choice = (
                token_id,
                round(price, 6),
                max(min_size, info.min_order_size),
                tick,
                bool(getattr(book, "neg_risk", False)),
                market.question,
            )
            break

    if choice is None:
        console.print("[red]No suitable cheap market with a book found for dry-run.[/red]")
        raise typer.Exit(1)
    token_id, price, size, tick, neg_risk, question = choice
    console.print(
        f"Placing test BUY {size:g} @ {price:.4f} on '{question[:44]}' "
        f"(far below mid, post-only) — it should rest, not fill..."
    )
    order_id = gateway.place_post_only(token_id, "BUY", price, size, tick, neg_risk)
    console.print(f"[green]Order placed: {order_id}[/green]")
    cancel_result = gateway.cancel(order_id)
    console.print(f"[green]Order cancelled: {cancel_result}[/green]")

    settings.reward_mm_live_dry_run_marker.write_text(datetime.now(tz=UTC).isoformat())
    console.print(
        "[bold green]Dry-run PASSED.[/bold green] Live connectivity, order placement and "
        "cancellation all work. B-1 live farming can now be enabled."
    )


@app.command()
def geoblock() -> None:
    """Print detected Polymarket geoblock status."""
    run_async(_geoblock())


async def _geoblock() -> None:
    settings = get_settings()
    async with GeoblockClient(settings) as client:
        result = await client.check()
    table = Table(title="Polymarket Geoblock")
    table.add_column("blocked")
    table.add_column("country")
    table.add_column("region")
    table.add_column("ip")
    table.add_row(
        str(result.blocked), result.country or "-", result.region or "-", result.ip or "-"
    )
    console.print(table)


if __name__ == "__main__":
    app()

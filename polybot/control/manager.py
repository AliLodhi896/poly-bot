from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from polybot.config import Settings


class GateError(Exception):
    """Raised when a start request fails a hard safety gate (live flags, dry-run, instance)."""


# Engine commands the manager can spawn. `reward-mm` is paper-only by construction: it never
# builds a signer and never submits an order, whatever the environment says.
_PAPER_COMMAND = "reward-mm"
_LIVE_COMMAND = "reward-mm-live"


@dataclass
class LaunchParams:
    markets: int = 1
    size: float = 30.0
    max_loss_usdc: float = 0.5
    max_exposure_usdc: float | None = None
    max_position_shares: float | None = None
    seconds: float | None = None
    market_ids: list[str] = field(default_factory=list)
    halt_on_stop_loss: bool = False
    arb_close_after_fill: bool = True


def _dry_run_fresh(settings: Settings, max_age_hours: float = 24.0) -> bool:
    marker = settings.reward_mm_live_dry_run_marker
    if not marker.exists():
        return False
    return (time.time() - marker.stat().st_mtime) <= max_age_hours * 3600


def _tail(path: Path, max_bytes: int = 4000) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


class BotProcessManager:
    """Owns at most one live-bot subprocess. Read-only w.r.t. trading; pure process control."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.proc: subprocess.Popen[bytes] | None = None
        self.params: dict[str, Any] | None = None
        self.started_at: float | None = None
        self.started_mode: str | None = None
        self.log_path = Path(".polybot-control-bot.log")
        self._log_fp: IO[bytes] | None = None

    def _running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def gate_problems(self) -> list[str]:
        """Configuration missing before REAL orders can be placed. Empty ⇒ live is intended.

        These are the deliberate, operator-set switches. Not having them is the normal state
        for someone trying the bot out, so they select the paper engine rather than refusing
        to run. What they never do is the reverse: once these ARE set, a start is live or it
        fails — see :meth:`start`.
        """
        problems: list[str] = []
        if not self.settings.live_enabled:
            problems.append("POLYBOT_LIVE_ENABLED is not enabled")
        if not self.settings.reward_mm_micro_live_enabled:
            problems.append("POLYBOT_REWARD_MM_MICRO_LIVE_ENABLED is not enabled")
        if not (self.settings.wallet_private_key or "").strip():
            problems.append("POLYBOT_WALLET_PRIVATE_KEY is not set")
        return problems

    def mode(self) -> str:
        """``"live"`` when the operator has configured live trading; otherwise ``"paper"``."""
        return "paper" if self.gate_problems() else "live"

    def refresh_dry_run(self, timeout: float = 180.0) -> dict[str, Any]:
        """Place + immediately cancel one tiny far-from-mid order, refreshing the live marker.

        Blocking (spawns the CLI's own dry-run). Real risk is a few cents for about a second.
        """
        argv = [sys.executable, "-m", "polybot", "reward-mm-live", "--dry-run"]
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, our own CLI
                argv,
                env=dict(os.environ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(Path.cwd()),
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "detail": "dry-run timed out"}
        if proc.returncode == 0 and _dry_run_fresh(self.settings):
            return {"ok": True, "detail": "verified"}
        tail = (proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        return {"ok": False, "detail": tail[-1] if tail else "dry-run failed"}

    def start(self, params: LaunchParams) -> dict[str, Any]:
        if self._running():
            raise GateError("bot is already running — stop it first")
        live = self.mode() == "live"
        if live:
            # The dry-run marker expires; a stale one must NOT quietly demote a configured live
            # run to paper. Refresh it (one tiny order, placed and cancelled) and fail loudly if
            # that does not work — a broken live wiring is an error, not a reason to paper-trade.
            if self.settings.reward_mm_live_require_dry_run and not _dry_run_fresh(self.settings):
                res = self.refresh_dry_run()
                if not res["ok"]:
                    raise GateError(f"live wiring check failed: {res['detail']}")
            # Cross-process guard: refuse if any live bot (UI- or terminal-launched) holds the
            # lock. Paper runs place no orders, so they are not subject to it.
            from polybot.control.lock import held_by

            holder = held_by(self.settings.live_lock_file)
            if holder is not None:
                raise GateError(
                    f"a live bot is already running (pid {holder}); "
                    "stop it before starting another"
                )

        # Every pinned market must be quoted, so the --markets target can never be
        # below the number of explicitly selected markets (real money on each).
        markets_target = max(int(params.markets), len(params.market_ids))

        argv = [
            sys.executable,
            "-m",
            "polybot",
            _LIVE_COMMAND if live else _PAPER_COMMAND,
            "--no-dashboard",
            "--markets",
            str(markets_target),
            "--size",
            str(float(params.size)),
            "--max-loss-usdc",
            str(float(params.max_loss_usdc)),
        ]
        if params.seconds:
            argv += ["--seconds", str(float(params.seconds))]

        env = dict(os.environ)
        if params.market_ids:
            env["POLYBOT_REWARD_MM_MARKET_WHITELIST"] = ",".join(params.market_ids)
        if params.max_exposure_usdc is not None:
            env["POLYBOT_REWARD_MM_MAX_EXPOSURE_USDC"] = str(float(params.max_exposure_usdc))
        if params.max_position_shares is not None:
            env["POLYBOT_REWARD_MM_MAX_POSITION_SHARES"] = str(float(params.max_position_shares))
        if params.halt_on_stop_loss:
            env["POLYBOT_REWARD_MM_HALT_MARKET_ON_STOP_LOSS"] = "true"
        if not params.arb_close_after_fill:
            env["POLYBOT_REWARD_MM_ARB_CLOSE_AFTER_FILL"] = "false"

        self._log_fp = self.log_path.open("ab")
        self._log_fp.write(
            f"\n=== launch {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"mode={'live' if live else 'paper'} markets={params.markets} "
            f"size={params.size} whitelist={params.market_ids or 'auto'} ===\n".encode()
        )
        self._log_fp.flush()
        self.proc = subprocess.Popen(  # noqa: S603 — fixed argv, our own CLI
            argv,
            env=env,
            stdout=self._log_fp,
            stderr=subprocess.STDOUT,
            cwd=str(Path.cwd()),
            # Own session: a terminal Ctrl+C does NOT reach the bot directly, so the only
            # stop path is the explicit stop() below (a single, clean SIGINT). Otherwise the
            # bot would get the terminal SIGINT *and* stop()'s SIGINT — the second one can
            # interrupt its order-cancel routine (KeyboardInterrupt bypasses its Exception guard).
            start_new_session=True,
        )
        self.params = {
            "markets": markets_target,
            "size": params.size,
            "max_loss_usdc": params.max_loss_usdc,
            "max_exposure_usdc": params.max_exposure_usdc,
            "max_position_shares": params.max_position_shares,
            "seconds": params.seconds,
            "market_ids": params.market_ids,
            "halt_on_stop_loss": params.halt_on_stop_loss,
            "arb_close_after_fill": params.arb_close_after_fill,
        }
        self.started_at = time.time()
        self.started_mode = "live" if live else "paper"
        return self.status()

    def stop(self) -> dict[str, Any]:
        if self._running() and self.proc is not None:
            # SIGINT → bot's asyncio.run raises KeyboardInterrupt → coordinator `finally`
            # cancels all resting orders before exit (graceful, never sells at a loss).
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self._close_log()
        result = self.status()
        self.proc = None
        self.params = None
        self.started_at = None
        self.started_mode = None
        return result

    def status(self) -> dict[str, Any]:
        running = self._running()
        problems = self.gate_problems()
        exit_code = None
        if self.proc is not None and not running:
            exit_code = self.proc.returncode
        return {
            "running": running,
            "pid": self.proc.pid if running and self.proc else None,
            "params": self.params if running else None,
            "started_at": self.started_at if running else None,
            # What a start would do now — and, while running, what it actually did.
            "mode": self.started_mode if running else self.mode(),
            # Live-configured starts refresh a stale marker themselves; this is for display.
            "dry_run_fresh": _dry_run_fresh(self.settings),
            # `gates_ok` describes LIVE configuration only; a start is never blocked by it.
            "gates_ok": not problems,
            "gate_problems": problems,
            "exit_code": exit_code,
            "last_log": _tail(self.log_path, 4000),
        }

    def _close_log(self) -> None:
        if self._log_fp is not None:
            try:
                self._log_fp.close()
            finally:
                self._log_fp = None

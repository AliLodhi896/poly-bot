"""The control plane picks paper vs live, and never places real orders by accident."""

from __future__ import annotations

import os
import time

import pytest

from polybot.config import Settings
from polybot.control import manager as control_manager
from polybot.control.manager import BotProcessManager, GateError, LaunchParams


class _FakeProc:
    """Stands in for the spawned engine: alive until explicitly reaped."""

    pid = 4242
    returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    """Capture the argv the manager would spawn instead of starting a real process."""
    calls: list[list[str]] = []

    def fake_popen(argv, **_kwargs):
        calls.append(list(argv))
        return _FakeProc()

    monkeypatch.setattr(control_manager.subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)
    return calls


def _settings(tmp_path, **overrides) -> Settings:
    base = {
        "live_enabled": False,
        "reward_mm_micro_live_enabled": False,
        "wallet_private_key": None,
        "reward_mm_live_require_dry_run": True,
        "reward_mm_live_dry_run_marker": tmp_path / "dry-run",
        "live_lock_file": tmp_path / "live.lock",
    }
    base.update(overrides)
    return Settings(**base)


def _live_settings(tmp_path) -> Settings:
    marker = tmp_path / "dry-run"
    marker.write_text("ok")
    return _settings(
        tmp_path,
        live_enabled=True,
        reward_mm_micro_live_enabled=True,
        wallet_private_key="0x" + "11" * 32,
    )


def test_default_environment_runs_the_paper_engine(tmp_path, spawned):
    """No credentials, no flags: the bot still starts — as paper."""
    m = BotProcessManager(_settings(tmp_path))

    assert m.mode() == "paper"
    assert m.gate_problems()  # live is not available, and says why

    status = m.start(LaunchParams(markets=1, size=20.0))

    assert spawned[0][3] == "reward-mm"
    assert status["mode"] == "paper"
    assert status["running"] is True
    assert status["gates_ok"] is False


def test_all_gates_satisfied_runs_the_live_engine(tmp_path, spawned):
    m = BotProcessManager(_live_settings(tmp_path))

    assert m.mode() == "live"
    assert m.gate_problems() == []

    status = m.start(LaunchParams(markets=1, size=20.0))

    assert spawned[0][3] == "reward-mm-live"
    assert status["mode"] == "live"
    assert status["gates_ok"] is True


@pytest.mark.parametrize(
    "missing",
    [
        {"live_enabled": False},
        {"reward_mm_micro_live_enabled": False},
        {"wallet_private_key": None},
        {"wallet_private_key": "   "},
    ],
)
def test_any_missing_live_gate_falls_back_to_paper(tmp_path, spawned, missing):
    """Every live prerequisite is load-bearing on its own — one missing means paper."""
    settings = _live_settings(tmp_path).model_copy(update=missing)
    m = BotProcessManager(settings)

    assert m.mode() == "paper"
    m.start(LaunchParams())

    assert spawned[0][3] == "reward-mm"


def _make_stale(settings) -> None:
    marker = settings.reward_mm_live_dry_run_marker
    stale = time.time() - 48 * 3600
    os.utime(marker, (stale, stale))


def test_stale_marker_is_refreshed_rather_than_demoting_the_run_to_paper(
    tmp_path, spawned, monkeypatch
):
    """A configured live run must never quietly become a paper run."""
    settings = _live_settings(tmp_path)
    _make_stale(settings)
    m = BotProcessManager(settings)
    refreshed: list[bool] = []

    def fake_refresh(timeout=180.0):
        refreshed.append(True)
        settings.reward_mm_live_dry_run_marker.write_text("ok")
        return {"ok": True, "detail": "verified"}

    monkeypatch.setattr(m, "refresh_dry_run", fake_refresh)

    assert m.mode() == "live"
    status = m.start(LaunchParams())

    assert refreshed == [True]
    assert spawned[0][3] == "reward-mm-live"
    assert status["mode"] == "live"


def test_failed_wiring_check_raises_instead_of_paper_trading(tmp_path, spawned, monkeypatch):
    """Broken live wiring is an error. Silently placing no orders would be worse."""
    settings = _live_settings(tmp_path)
    _make_stale(settings)
    m = BotProcessManager(settings)
    monkeypatch.setattr(
        m, "refresh_dry_run", lambda timeout=180.0: {"ok": False, "detail": "no allowance"}
    )

    with pytest.raises(GateError, match="live wiring check failed: no allowance"):
        m.start(LaunchParams())

    assert spawned == []


def test_fresh_marker_needs_no_wiring_check(tmp_path, spawned, monkeypatch):
    m = BotProcessManager(_live_settings(tmp_path))
    monkeypatch.setattr(
        m, "refresh_dry_run", lambda timeout=180.0: pytest.fail("should not re-verify")
    )

    m.start(LaunchParams())

    assert spawned[0][3] == "reward-mm-live"


def test_live_start_refuses_when_another_live_bot_holds_the_lock(tmp_path, spawned, monkeypatch):
    import polybot.control.lock as lock_mod

    monkeypatch.setattr(lock_mod, "held_by", lambda _path: 999)
    m = BotProcessManager(_live_settings(tmp_path))

    with pytest.raises(GateError, match="already running"):
        m.start(LaunchParams())

    assert spawned == []


def test_paper_start_ignores_the_live_lock(tmp_path, spawned, monkeypatch):
    """The lock exists to stop two live bots quoting the same wallet. Paper places no orders."""
    import polybot.control.lock as lock_mod

    monkeypatch.setattr(lock_mod, "held_by", lambda _path: 999)
    m = BotProcessManager(_settings(tmp_path))

    m.start(LaunchParams())

    assert spawned[0][3] == "reward-mm"


def test_second_start_is_refused_while_one_is_running(tmp_path, spawned):
    m = BotProcessManager(_settings(tmp_path))
    m.start(LaunchParams())

    with pytest.raises(GateError, match="already running"):
        m.start(LaunchParams())

    assert len(spawned) == 1


def test_pinned_markets_raise_the_markets_target(tmp_path, spawned):
    """Every explicitly selected market must be quoted — real money rides on each."""
    m = BotProcessManager(_settings(tmp_path))

    m.start(LaunchParams(markets=1, market_ids=["a", "b", "c"]))

    argv = spawned[0]
    assert argv[argv.index("--markets") + 1] == "3"


def test_cli_accepts_every_flag_the_manager_spawns_with() -> None:
    """The manager's spawn argv and the CLI must not drift apart: a flag the CLI
    dropped (e.g. --no-dashboard) makes every UI-launched run exit with a usage error."""
    import re

    from typer.testing import CliRunner

    from polybot.cli import app as cli_app

    # CI terminals force rich colors/wrapping that split flag names with ANSI codes; make the
    # help output environment-independent before matching.
    runner = CliRunner(env={"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"})
    for command in ("reward-mm", "reward-mm-live"):
        result = runner.invoke(cli_app, [command, "--help"])
        assert result.exit_code == 0
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        for flag in ("--no-dashboard", "--markets", "--size", "--max-loss-usdc", "--seconds"):
            assert flag in plain, f"{command} lost {flag}"

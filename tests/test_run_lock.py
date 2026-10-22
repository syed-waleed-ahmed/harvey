"""One heartbeat at a time, and dashboard controls that find it.

The run lock, not a bare PID, says Harvey is running: it's gone the moment
the process ends however it ends, so a stale PID file (a crash, a reboot, a
PID handed to another program) never reads as "running" and never breaks the
dashboard. Start launches `harvey run`, not the CLI's help screen.
"""

import asyncio
import os
import signal
import subprocess
import sys
import textwrap

import pytest

import harvey.dashboard as dash
from harvey import pidfile


@pytest.fixture
def pid_path(tmp_path):
    return tmp_path / "data" / "harvey.pid"


def _hold_lock_in_child(pid_path):
    """A separate process that takes the run lock and waits to be killed.
    Returns (proc, holder_pid): on Windows a venv's python.exe is a launcher,
    so proc.pid isn't the interpreter holding the lock."""
    code = textwrap.dedent(f"""
        import os, time
        from harvey import pidfile
        pidfile.claim({str(pid_path)!r})
        print(os.getpid(), flush=True)
        time.sleep(60)
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    return proc, int(proc.stdout.readline())


def _hard_kill(pid):
    os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))  # TerminateProcess on Windows


# ── the lock ──

def test_claim_records_pid_and_blocks_a_second_claim(pid_path):
    fd = pidfile.claim(pid_path)
    try:
        assert pidfile.running_pid(pid_path) == os.getpid()
        with pytest.raises(pidfile.AlreadyRunning) as e:
            pidfile.claim(pid_path)
        assert e.value.pid == os.getpid()
    finally:
        pidfile.release(fd)
    assert pidfile.running_pid(pid_path) is None
    pidfile.release(pidfile.claim(pid_path))  # free again


def test_no_file_means_not_running(pid_path):
    assert pidfile.running_pid(pid_path) is None


@pytest.mark.parametrize("content", ["4000000", "not a pid", "", "-5"])
def test_stale_file_without_the_lock_is_not_running(pid_path, content):
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text(content, encoding="utf-8")
    assert pidfile.running_pid(pid_path) is None
    pidfile.release(pidfile.claim(pid_path))  # and a new heartbeat may start


def test_lock_is_gone_when_the_holder_is_killed(pid_path):
    proc, holder = _hold_lock_in_child(pid_path)
    try:
        assert pidfile.running_pid(pid_path) == holder
        with pytest.raises(pidfile.AlreadyRunning):
            pidfile.claim(pid_path)
    finally:
        _hard_kill(holder)
        proc.wait(timeout=10)
    # Killed outright, no cleanup ran, the PID is still in the file: not running.
    assert pidfile.running_pid(pid_path) is None


def test_second_heartbeat_refuses_to_start(pid_path, monkeypatch, capsys):
    import harvey.main as main_mod
    monkeypatch.setattr(main_mod, "_needs_setup", lambda: False)
    monkeypatch.setattr(pidfile, "PID_FILE", pid_path)
    fd = pidfile.claim(pid_path)
    try:
        with pytest.raises(SystemExit) as e:
            main_mod.main()
        assert e.value.code == 1
        assert "already running" in capsys.readouterr().out
    finally:
        pidfile.release(fd)


# ── dashboard controls ──

@pytest.fixture
def dash_env(pid_path, monkeypatch):
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(dash, "PID_FILE", pid_path)
    monkeypatch.setattr(dash, "LOG_FILE", pid_path.parent / "harvey.log")
    monkeypatch.setattr(dash, "_harvey_process", None)
    monkeypatch.setattr(dash, "_harvey_started_at", None)
    return pid_path


def test_start_launches_the_heartbeat_not_the_help_screen(dash_env, monkeypatch):
    launched = {}

    class FakePopen:
        pid = 4321

        def __init__(self, args, **kw):
            launched["args"] = args

        def poll(self):
            return None

    monkeypatch.setattr(dash.subprocess, "Popen", FakePopen)
    result = asyncio.run(dash.start_harvey())
    assert result == {"success": True, "pid": 4321}
    assert launched["args"][1:] == ["-m", "harvey", "run"]
    assert not dash_env.exists()  # the heartbeat writes its own PID
    assert asyncio.run(dash.get_harvey_status())["running"] is True


def test_a_child_that_exited_is_not_running(dash_env, monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    monkeypatch.setattr(dash, "_harvey_process", proc)  # still holds its handle
    assert asyncio.run(dash.get_harvey_status())["running"] is False
    assert dash._harvey_process is None


def test_stale_pid_file_does_not_break_the_controls(dash_env):
    dash_env.write_text("4000000", encoding="utf-8")
    assert asyncio.run(dash.get_harvey_status()) == {
        "running": False, "pid": None, "started_at": None}
    assert asyncio.run(dash.stop_harvey())["success"] is False


def test_status_sees_a_heartbeat_started_from_a_terminal(dash_env):
    proc, holder = _hold_lock_in_child(dash_env)
    try:
        assert asyncio.run(dash.get_harvey_status())["pid"] == holder
        assert asyncio.run(dash.start_harvey())["success"] is False  # no second one
    finally:
        _hard_kill(holder)
        proc.wait(timeout=10)


def test_stop_ends_the_heartbeat(dash_env):
    proc, _ = _hold_lock_in_child(dash_env)
    try:
        assert asyncio.run(dash.stop_harvey()) == {"success": True}
        assert proc.wait(timeout=10) is not None
        assert asyncio.run(dash.get_harvey_status())["running"] is False
    finally:
        if proc.poll() is None:
            proc.kill()

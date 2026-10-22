"""One heartbeat at a time, and a way for the dashboard to find it.

`harvey run` holds an OS lock on data/harvey.pid for as long as it runs and
writes its PID into the file. The lock, not the PID, is what says Harvey is
running: the OS drops it the moment the process ends, however it ends, so a
crash, a hard kill or a reboot can't leave a stale "running" behind, and a
PID the OS has since given to another program is never taken for Harvey.
A bare PID check can't tell those apart, and on Windows `os.kill(pid, 0)`
isn't a liveness check at all (signal 0 is CTRL_C_EVENT there).
"""

import os
import sys
import time
from pathlib import Path

from harvey.paths import PROJECT_ROOT

PID_FILE = PROJECT_ROOT / "data" / "harvey.pid"

# Windows locks byte ranges, and nobody else can read a locked byte: lock one
# far past the PID so the dashboard can still read the PID itself.
_LOCK_OFFSET = 1 << 20


class AlreadyRunning(RuntimeError):
    def __init__(self, pid: int | None):
        super().__init__(f"Harvey is already running (PID {pid or 'unknown'}).")
        self.pid = pid


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def _read_pid(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def claim(path: Path | None = None) -> int:
    """Take the run lock and record this process's PID. Returns the open fd,
    which holds the lock until release() or exit. Raises AlreadyRunning."""
    path = Path(path or PID_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    # A few tries: running_pid() takes the lock for an instant to test it.
    for attempt in range(3):
        if _try_lock(fd):
            break
        if attempt < 2:
            time.sleep(0.05)
    else:
        os.close(fd)
        raise AlreadyRunning(_read_pid(path))
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode("ascii"))
    return fd


def release(fd: int) -> None:
    """Clear the PID and drop the lock."""
    try:
        os.ftruncate(fd, 0)
    except OSError:
        pass
    os.close(fd)  # closing drops the lock on every platform


def running_pid(path: Path | None = None) -> int | None:
    """The PID of the heartbeat holding the run lock, or None if none is.
    (Also None in the instant before a new holder writes its PID; claim()
    still refuses a second heartbeat then.)"""
    path = Path(path or PID_FILE)
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return None
    try:
        if _try_lock(fd):
            _unlock(fd)
            return None
    finally:
        os.close(fd)
    # Held. The holder writes its PID right after locking; give it a moment.
    for _ in range(5):
        pid = _read_pid(path)
        if pid:
            return pid
        time.sleep(0.02)
    return None

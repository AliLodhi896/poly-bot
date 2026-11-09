"""A simple advisory single-instance file lock (Unix ``flock``).

Held by the live bot for its whole lifetime; the OS releases it automatically when the
process exits (even on a crash), so there are no stale locks. The control manager tests
the lock non-intrusively before spawning, to refuse a second concurrent live bot.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import IO


class SingleInstanceLock:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fp: IO[str] | None = None

    def acquire(self) -> bool:
        """Take the exclusive lock and write our pid. Returns False if already held."""
        fp = self.path.open("a+")
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fp.close()
            return False
        fp.seek(0)
        fp.truncate()
        fp.write(str(os.getpid()))
        fp.flush()
        self._fp = fp
        return True

    def release(self) -> None:
        if self._fp is not None:
            try:
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
            finally:
                self._fp.close()
                self._fp = None


def held_by(path: Path | str) -> int | None:
    """Return the pid holding the lock, or None if it is free. Does not keep the lock."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        fp = p.open("a+")
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fp.seek(0)
            try:
                return int(fp.read().strip() or "0") or -1
            except (ValueError, OSError):
                return -1
        # We got the lock → nobody held it. Release immediately.
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        return None
    finally:
        fp.close()

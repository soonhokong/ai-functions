"""Process- and thread-safe locks for shared compiler caches."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_LOCKS_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[Path, threading.Lock] = {}


def _local_lock(path: Path) -> threading.Lock:
    resolved = path.resolve()
    with _LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(resolved, threading.Lock())


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialize work across threads and Unix processes using ``flock``."""
    local_lock = _local_lock(path)
    with local_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as lock_file:
            try:
                import fcntl
            except ImportError as exc:  # pragma: no cover - guarded by platform support
                raise RuntimeError("verified compilation requires Unix file locking") from exc

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

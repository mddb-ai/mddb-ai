from __future__ import annotations

"""Cross-platform file lock.

POSIX uses ``fcntl.flock``, Windows uses ``msvcrt.locking``. The default
behavior of ``with FileLock(path):`` is to acquire an exclusive lock. Shared
locks only have effect on POSIX; Windows always behaves as exclusive (a
limitation of the Windows API).
"""

import os
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import Final

from mddbai.core.errors import LockTimeoutError

_POLL_INTERVAL_S: Final = 0.05


class FileLock:
    """File-level lock.

    Args:
        path: Path to the resource being protected. The lock itself is
            placed on the ``<path>.lock`` file.
        timeout_s: Acquisition timeout. Raises ``LockTimeoutError`` on exceed.
    """

    def __init__(self, path: Path, *, timeout_s: float = 30.0) -> None:
        self._target = Path(path)
        self._lock_path = self._target.with_name(self._target.name + ".lock")
        self._timeout_s = timeout_s
        self._fd: int | None = None
        self._mode: str = "exclusive"

    @property
    def path(self) -> Path:
        return self._lock_path

    def acquire_exclusive(self) -> None:
        self._acquire(shared=False)

    def acquire_shared(self) -> None:
        self._acquire(shared=True)

    def _acquire(self, *, shared: bool) -> None:
        if self._fd is not None:
            return
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._timeout_s
        last_err: OSError | None = None
        fd: int | None = None
        try:
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            while True:
                try:
                    self._platform_lock(fd, shared=shared)
                    self._fd = fd
                    self._mode = "shared" if shared else "exclusive"
                    return
                except OSError as exc:
                    last_err = exc
                    if time.monotonic() >= deadline:
                        os.close(fd)
                        raise LockTimeoutError(
                            f"timeout acquiring {self._lock_path}",
                            timeout_s=self._timeout_s,
                        ) from last_err
                    time.sleep(_POLL_INTERVAL_S)
        except LockTimeoutError:
            raise
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            raise LockTimeoutError(
                f"failed opening lock file {self._lock_path}"
            ) from exc

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            self._platform_unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    # context manager protocol -------------------------------------------------

    def __enter__(self) -> FileLock:
        self.acquire_exclusive()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    # platform abstraction ---------------------------------------------------

    if sys.platform == "win32":  # pragma: no cover - platform branch

        def _platform_lock(self, fd: int, *, shared: bool) -> None:
            import msvcrt

            # Windows: msvcrt.locking has no shared lock; everything is exclusive.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

        def _platform_unlock(self, fd: int) -> None:
            import msvcrt

            try:
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass

    else:  # POSIX

        def _platform_lock(self, fd: int, *, shared: bool) -> None:
            import fcntl

            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(fd, mode | fcntl.LOCK_NB)

        def _platform_unlock(self, fd: int) -> None:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass

    @staticmethod
    def break_stale(path: Path, max_age_s: float) -> bool:
        """Clean up a stale lock file. Uses mtime only (no pid-based check).

        Returns: True if the lock file was actually deleted.
        """

        lock_path = Path(path).with_name(Path(path).name + ".lock")
        if not lock_path.exists():
            return False
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            return False
        if age < max_age_s:
            return False
        try:
            lock_path.unlink()
            return True
        except OSError:
            return False


__all__ = ["FileLock"]

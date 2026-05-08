from __future__ import annotations

"""Atomic file writes.

Even if the process dies mid-write, the target file always shows either the
``pre-write state`` or the ``post-write state`` — never something in between.
The mechanism is ``fsync to a temp path, then os.replace``.
"""

import errno
import os
import secrets
import sys
import time
from pathlib import Path

from mddbai.core.errors import AtomicWriteError


# M.4: under multi-process races, Windows ``os.replace`` may raise
# PermissionError (WinError 5) or ENOENT. Retry with a short backoff.
_REPLACE_RETRY_ATTEMPTS = 8
_REPLACE_RETRY_BACKOFF_S = 0.025


def _tmp_path(target: Path) -> Path:
    suffix = f".tmp.{os.getpid()}.{secrets.token_hex(4)}"
    return target.with_name(target.name + suffix)


def _fsync_file(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP}:
            return
        raise


def fsync_dir(path: Path) -> None:
    """Directory fsync. Effective on POSIX only; a no-op on Windows."""

    if sys.platform == "win32":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        _fsync_file(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, *, fsync: bool = True) -> None:
    """Atomically write ``data`` to ``path``."""

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fh.write(data)
            fh.flush()
            if fsync:
                _fsync_file(fh.fileno())
        last_exc: OSError | None = None
        for attempt in range(_REPLACE_RETRY_ATTEMPTS):
            try:
                os.replace(tmp, path)
                last_exc = None
                break
            except PermissionError as exc:
                # Windows: another process may temporarily hold the target (WinError 5/32).
                last_exc = exc
                time.sleep(_REPLACE_RETRY_BACKOFF_S * (attempt + 1))
            except FileNotFoundError as exc:
                # tmp disappeared — another process may have cleaned it up.
                last_exc = exc
                break
            except OSError as exc:
                last_exc = exc
                break
        if last_exc is not None:
            raise AtomicWriteError(
                f"replace failed: {tmp} -> {path}", path=str(path)
            ) from last_exc
        if fsync:
            try:
                fsync_dir(parent)
            except OSError as exc:  # pragma: no cover - platform dependent
                raise AtomicWriteError(f"fsync_dir failed: {parent}") from exc
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, content: str, *, fsync: bool = True) -> None:
    """Atomic text write. Encoding is fixed to UTF-8; line endings are ``\\n``."""

    atomic_write_bytes(path, content.encode("utf-8"), fsync=fsync)


__all__ = ["atomic_write_bytes", "atomic_write_text", "fsync_dir"]

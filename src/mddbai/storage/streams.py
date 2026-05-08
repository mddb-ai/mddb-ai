from __future__ import annotations

"""Stream utilities. Memory-efficient read helpers."""

from collections.abc import Iterator
from pathlib import Path


def iter_lines(path: Path, *, encoding: str = "utf-8") -> Iterator[str]:
    """Lazily yield lines from ``path``. Trailing newlines are stripped."""

    with Path(path).open("r", encoding=encoding, newline="") as fh:
        for line in fh:
            yield line.rstrip("\r\n")


def read_range(path: Path, offset: int, length: int) -> bytes:
    """Return bytes in range [offset, offset+length) from the file."""

    if offset < 0 or length < 0:
        raise ValueError("offset and length must be non-negative")
    with Path(path).open("rb") as fh:
        fh.seek(offset)
        return fh.read(length)


__all__ = ["iter_lines", "read_range"]

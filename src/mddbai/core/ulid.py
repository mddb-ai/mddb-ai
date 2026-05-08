from __future__ import annotations

"""ULID generator.

26-character Crockford Base32 encoded. The first 10 characters are the ms
epoch and the trailing 16 carry 80 bits of randomness. Calls within the
same millisecond bump the random portion of the previous ULID by 1 to
guarantee monotonicity. If wall-clock time goes backwards the previous
ms is reused.
"""

import os
import secrets
import threading
import time

from .types import RecordId, Timestamp

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE_TABLE: dict[str, int] = {c: i for i, c in enumerate(_CROCKFORD)}
# Crockford aliases (input stability for some characters).
_DECODE_TABLE.update({"O": 0, "I": 1, "L": 1})

_TIME_LEN = 10
_RAND_LEN = 16
_TOTAL_LEN = _TIME_LEN + _RAND_LEN
_MAX_TIME = (1 << 48) - 1
_MAX_RAND = (1 << 80) - 1

_state_lock = threading.Lock()
_last_ms: int = -1
_last_rand: int = 0


def _encode(value: int, length: int) -> str:
    if value < 0:
        raise ValueError("negative value cannot be encoded")
    out = ["0"] * length
    for i in range(length - 1, -1, -1):
        out[i] = _CROCKFORD[value & 0x1F]
        value >>= 5
    if value:
        raise ValueError("value too large for the requested length")
    return "".join(out)


def _decode(text: str) -> int:
    acc = 0
    for ch in text.upper():
        try:
            acc = (acc << 5) | _DECODE_TABLE[ch]
        except KeyError as exc:
            raise ValueError(f"invalid Crockford Base32 char: {ch!r}") from exc
    return acc


def new_ulid(*, now_ms: int | None = None) -> RecordId:
    """Generate a 26-character monotonically increasing ULID.

    Args:
        now_ms: ms epoch override for tests. ``None`` uses the system clock.

    Returns:
        A 26-char RecordId. Within the same process its sort order matches creation order.
    """

    global _last_ms, _last_rand

    ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if ms < 0 or ms > _MAX_TIME:
        raise ValueError(f"ms epoch out of range: {ms}")

    with _state_lock:
        if ms <= _last_ms:
            ms = _last_ms
            _last_rand = (_last_rand + 1) & _MAX_RAND
            if _last_rand == 0:
                # If the 80-bit random part wraps inside one ms, bump ms by 1.
                ms += 1
                _last_rand = int.from_bytes(os.urandom(10), "big")
                _last_ms = ms
            else:
                _last_ms = ms
        else:
            _last_ms = ms
            _last_rand = secrets.randbits(80)

        rand = _last_rand

    return RecordId(_encode(ms, _TIME_LEN) + _encode(rand, _RAND_LEN))


def parse_ulid(s: str) -> tuple[Timestamp, bytes]:
    """Decompose a ULID string into (nanosecond timestamp, 10 random bytes)."""

    if len(s) != _TOTAL_LEN:
        raise ValueError(f"ULID must be {_TOTAL_LEN} chars, got {len(s)}")
    ms = _decode(s[:_TIME_LEN])
    rand = _decode(s[_TIME_LEN:])
    if rand > _MAX_RAND:
        raise ValueError("random part overflow")
    rand_bytes = rand.to_bytes(10, "big")
    return Timestamp(ms * 1_000_000), rand_bytes


def _reset_state_for_testing() -> None:
    """Reset monotonic state. For deterministic-ms-injection tests only."""

    global _last_ms, _last_rand
    with _state_lock:
        _last_ms = -1
        _last_rand = 0


__all__ = ["new_ulid", "parse_ulid"]

from __future__ import annotations

"""Hashing helpers. xxhash is preferred, with a blake2b fallback when unavailable."""

from typing import TYPE_CHECKING

from .types import ShardId

try:  # pragma: no cover - environment dependent
    import xxhash as _xxhash

    def xxh64(data: bytes) -> int:
        """xxh64 64-bit unsigned integer."""

        return _xxhash.xxh64(data).intdigest()

except ImportError:  # pragma: no cover
    import hashlib

    def xxh64(data: bytes) -> int:
        digest = hashlib.blake2b(data, digest_size=8).digest()
        return int.from_bytes(digest, "big", signed=False)


if TYPE_CHECKING:
    pass


def shard_of(key: str, fanout: int) -> ShardId:
    """Map a key to one of ``fanout`` shards.

    Args:
        key: hash input string.
        fanout: must be a power of 16 such as 16, 256, 4096 (the hex prefix length is derived from it).

    Returns:
        Shard identifier (lowercase hex string).
    """

    if fanout <= 0:
        raise ValueError("fanout must be positive")
    if fanout & (fanout - 1) != 0:
        raise ValueError("fanout must be a power of two for prefix sharding")
    h = xxh64(key.encode("utf-8"))
    bucket = h % fanout
    width = max(1, (fanout - 1).bit_length() // 4)
    return ShardId(format(bucket, f"0{width}x"))


def content_hash(s: str) -> str:
    """Content hash of a string (16-character hex)."""

    return format(xxh64(s.encode("utf-8")), "016x")


__all__ = ["content_hash", "shard_of", "xxh64"]

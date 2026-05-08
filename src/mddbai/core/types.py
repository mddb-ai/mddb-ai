from __future__ import annotations

"""Common type definitions.

All new domain types belong here. NewType is used liberally so semantic
distinctions are visible under mypy --strict.
"""

from typing import NewType

RecordId = NewType("RecordId", str)
"""26-character ULID string."""

ShardId = NewType("ShardId", str)
"""Shard identifier (hex string derived from a hash prefix)."""

Timestamp = NewType("Timestamp", int)
"""Epoch in nanoseconds."""

Strength = NewType("Strength", float)
"""Activation/connection strength in the 0.0..1.0 range."""

Lsn = NewType("Lsn", int)
"""WAL sequence number. Monotonically increasing non-negative integer."""

TableName = NewType("TableName", str)
FieldName = NewType("FieldName", str)
TxnId = NewType("TxnId", str)


__all__ = [
    "FieldName",
    "Lsn",
    "RecordId",
    "ShardId",
    "Strength",
    "TableName",
    "Timestamp",
    "TxnId",
]

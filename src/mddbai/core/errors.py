from __future__ import annotations

"""MDDB exception hierarchy.

Every user-defined exception descends from ``MddbError``. When wrapping
external exceptions, use the ``raise CustomError(...) from original``
pattern to preserve ``__cause__``. ``__str__`` prints the cause chain
alongside the message to aid debugging.
"""

from typing import Any


class MddbError(Exception):
    """Root of every MDDB exception."""

    def __init__(self, message: str = "", **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context)

    def __str__(self) -> str:
        parts: list[str] = []
        head = self.message or self.__class__.__name__
        if self.context:
            ctx = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
            head = f"{head} ({ctx})"
        parts.append(head)
        cause = self.__cause__
        depth = 0
        while cause is not None and depth < 8:
            parts.append(f"  caused by: {type(cause).__name__}: {cause}")
            cause = cause.__cause__
            depth += 1
        return "\n".join(parts)


# Storage / IO ---------------------------------------------------------------


class StorageError(MddbError):
    """Root of storage IO errors."""


class CorruptedFileError(StorageError):
    """The file is corrupted or not in the expected format."""


class AtomicWriteError(StorageError):
    """IO failure during an atomic write."""


class LockTimeoutError(StorageError):
    """File-lock acquisition timed out."""


# Codec ----------------------------------------------------------------------


class CodecError(MddbError):
    """Root of markdown / frontmatter encoding errors."""


class FrontmatterParseError(CodecError):
    """Frontmatter YAML parsing failed."""


class InvalidKeyError(CodecError):
    """Stage N.2.1 — record key violates path / length / empty constraints."""


class InvalidTimestampError(CodecError):
    """Stage N.2.2 — timestamp is negative, zero, empty, or otherwise invalid."""


class SchemaValidationError(CodecError):
    """Schema validation failed."""


# Index ----------------------------------------------------------------------


class IndexError(MddbError):  # noqa: A001 - shadows builtin intentionally
    """Root of index errors."""


class KeyNotFoundError(IndexError):
    """The given key is not present in the index."""


class IndexCorruptedError(IndexError):
    """Index structure is corrupted."""


# Transaction ---------------------------------------------------------------


class TxnError(MddbError):
    """Root of transaction errors."""


class ConflictError(TxnError):
    """Optimistic lock conflict."""


class AbortedError(TxnError):
    """The transaction was aborted."""


# Brain / Config ------------------------------------------------------------


class BrainError(MddbError):
    """Failure inside the brain layer (decay, link, summary, etc.)."""


class PalaceNotInitializedError(BrainError):
    """Stage AA.2 — read/write attempted in strict mode while INDEX.md is missing.

    Resolution: call ``db.init_palace(...)`` followed by ``db.confirm_init_palace(...)``.
    """


class ConfigError(MddbError):
    """Configuration loading or validation failure."""


# Security -----------------------------------------------------------------


class SecurityError(MddbError):
    """Root of RBAC / audit / cryptography errors."""


class PermissionDeniedError(SecurityError):
    """Permission violation."""


class AuditTamperedError(SecurityError):
    """Audit chain tampering detected."""


class CryptoError(SecurityError):
    """Encryption or decryption failure."""


__all__ = [
    "AbortedError",
    "AtomicWriteError",
    "AuditTamperedError",
    "BrainError",
    "CodecError",
    "ConfigError",
    "ConflictError",
    "CorruptedFileError",
    "CryptoError",
    "FrontmatterParseError",
    "IndexCorruptedError",
    "IndexError",
    "InvalidKeyError",
    "InvalidTimestampError",
    "KeyNotFoundError",
    "LockTimeoutError",
    "MddbError",
    "PalaceNotInitializedError",
    "PermissionDeniedError",
    "SchemaValidationError",
    "SecurityError",
    "StorageError",
    "TxnError",
]

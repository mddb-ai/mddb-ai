from __future__ import annotations

"""2026-05-07 — Strict retrieval guardrails (CLI).

If any of env / flag / config is on, strict mode activates. It blocks
whole-drawer dumps in take and large section dumps, and makes recall
accept only section-level routes.

Principle alignment:
- §3 (no full scan) — block drawer-only take.
- §10 (no drawer dump) — large section also requires explicit bypass.
- D8 (exact-section bytes) — warnings go to stderr, marker for tracking
  bypass counts.
"""

import os
import sys

# 64KB — estimated safe upper bound for a single section to enter AI context.
# Adjust via the ``MDDBAI_STRICT_LARGE_BYTES`` environment variable (default 65536).
DEFAULT_LARGE_SECTION_BYTES = 64 * 1024

STRICT_ENV = "MDDBAI_STRICT_RETRIEVAL"
LARGE_BYTES_ENV = "MDDBAI_STRICT_LARGE_BYTES"
BYPASS_WARN = "strict retrieval bypass"


def is_truthy_env(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def strict_active(*, flag: bool = False, config_strict: bool = False) -> bool:
    """Whether strict mode is active.

    Any of the three set to True means strict.
    1. ``flag``: CLI ``--strict``
    2. ``config_strict``: ``MddbConfig.navigation_strict``
    3. ``MDDBAI_STRICT_RETRIEVAL`` env (1/true/yes/on)
    """

    if flag or config_strict:
        return True
    return is_truthy_env(os.environ.get(STRICT_ENV))


def large_section_threshold() -> int:
    """Large-section byte threshold (configurable via env)."""

    raw = os.environ.get(LARGE_BYTES_ENV)
    if raw is None:
        return DEFAULT_LARGE_SECTION_BYTES
    try:
        v = int(raw.strip())
    except ValueError:
        return DEFAULT_LARGE_SECTION_BYTES
    return v if v > 0 else DEFAULT_LARGE_SECTION_BYTES


def warn_bypass(reason: str, *, size_bytes: int | None = None) -> None:
    """A strict bypass happened — write a standard one-line warning to stderr.

    Always emitted when the AI / user uses ``--allow-large-dump`` so it can be
    used later for audit / split signals.
    """

    extra = f" size={size_bytes}B" if size_bytes is not None else ""
    print(f"warning: {BYPASS_WARN}: {reason}{extra}", file=sys.stderr)


__all__ = [
    "BYPASS_WARN",
    "DEFAULT_LARGE_SECTION_BYTES",
    "LARGE_BYTES_ENV",
    "STRICT_ENV",
    "is_truthy_env",
    "large_section_threshold",
    "strict_active",
    "warn_bypass",
]

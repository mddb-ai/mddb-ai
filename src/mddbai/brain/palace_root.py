from __future__ import annotations

"""Palace root identity — ``<data_dir>/_palace.md``.

Identity of the entire memory palace (purpose / scale / axes / fallback /
created_at) is written into a single file at the data_dir root.

Design principles:
- D1 aligned — zero LLM calls. Pure disk read/write.
- D2 aligned — semantic decisions belong to the AI. MDDB only provides
  *space*.
- D7 aligned — folder structure is decided by the AI. This file is the
  SSOT of the data_dir root identity.
- `_INDEX.md` is *not written*. The disk listing itself is the index
  (user decision 2026-05-06).
- `transactional_rmw` provides atomicity + multi-writer safety.
- Idempotent: calling twice with the same content is fine. Different
  content raises ConflictError.
"""

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mddbai.codec.frontmatter import parse as _fm_parse
from mddbai.codec.frontmatter import render as _fm_render
from mddbai.core.errors import ConflictError
from mddbai.storage.transactional import transactional_rmw

PALACE_ROOT_REL = "_palace.md"
"""Path of the palace root file relative to data_dir."""

VALID_AXES = frozenset({"path", "time", "topic", "person", "free"})
VALID_SCALES = frozenset({"100", "1k", "10k", "100k", "1M+"})
VALID_FALLBACKS = frozenset({"auto_create", "unsorted"})


@dataclass(frozen=True)
class PalaceRootConfig:
    """The four palace-root identity fields.

    Attributes:
        purpose: One-line purpose of this palace. Must not be empty.
        scale: Expected record scale. One of VALID_SCALES.
        axes: Natural classification axes. At least one of VALID_AXES.
        fallback: Policy for unfamiliar patterns. One of VALID_FALLBACKS.
    """

    purpose: str
    scale: str
    axes: tuple[str, ...]
    fallback: str

    def __post_init__(self) -> None:
        """Validate inputs. Raises ValueError on invalid values."""
        if not self.purpose.strip():
            raise ValueError("purpose must not be empty")
        if self.scale not in VALID_SCALES:
            raise ValueError(
                f"scale must be one of {sorted(VALID_SCALES)} (got {self.scale!r})"
            )
        if not self.axes:
            raise ValueError("axes must contain at least 1 entry")
        unknown = set(self.axes) - VALID_AXES
        if unknown:
            raise ValueError(
                f"unknown axes: {sorted(unknown)}. "
                f"allowed: {sorted(VALID_AXES)}"
            )
        if self.fallback not in VALID_FALLBACKS:
            raise ValueError(
                f"fallback must be one of {sorted(VALID_FALLBACKS)} (got {self.fallback!r})"
            )


def palace_root_path(data_dir: Path) -> Path:
    """``<data_dir>/_palace.md`` path."""
    return Path(data_dir) / PALACE_ROOT_REL


def has_palace_root(data_dir: Path) -> bool:
    """Whether ``_palace.md`` exists in data_dir."""
    return palace_root_path(data_dir).exists()


def read_palace_root(data_dir: Path) -> PalaceRootConfig | None:
    """Read the frontmatter of ``_palace.md`` and return a PalaceRootConfig.

    Returns None if the file is missing. Restores the four frontmatter
    fields (purpose/scale/axes/fallback) exactly.

    Returns:
        PalaceRootConfig, or None if the file is missing.

    Raises:
        ValueError: If the file exists but the four fields are invalid.
    """
    p = palace_root_path(data_dir)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    front, _ = _fm_parse(text)
    axes_raw = front.get("axes", [])
    axes: tuple[str, ...] = (
        tuple(axes_raw) if isinstance(axes_raw, list) else (str(axes_raw),)
    )
    return PalaceRootConfig(
        purpose=str(front.get("purpose", "")),
        scale=str(front.get("scale", "")),
        axes=axes,
        fallback=str(front.get("fallback", "")),
    )


def write_palace_root(data_dir: Path, cfg: PalaceRootConfig) -> Path:
    """Write ``_palace.md`` at the data_dir root (atomic + multi-writer safe).

    Idempotency policy:
    - Newly write the file if it does not exist.
    - If it exists and the content is the same, do nothing (no-op, idempotent OK).
    - If it exists with **different** content, raise ConflictError.

    Frontmatter fields:
        _kind: palace_root
        _authored_by: palace_root_init
        purpose / scale / axes / fallback / created_at

    Args:
        data_dir: Database root folder.
        cfg: Validated PalaceRootConfig.

    Returns:
        Path of the file that was written (or already existed).

    Raises:
        ConflictError: If an existing file already has different content.
    """
    p = palace_root_path(data_dir)

    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _mutator(existing_text: str) -> str:
        """Check existing content; keep it if identical, raise ConflictError otherwise."""
        if existing_text:
            # Existing file present — compare the four fields
            front, _ = _fm_parse(existing_text)
            existing_axes_raw = front.get("axes", [])
            existing_axes: tuple[str, ...] = (
                tuple(existing_axes_raw)
                if isinstance(existing_axes_raw, list)
                else (str(existing_axes_raw),)
            )
            same = (
                str(front.get("purpose", "")) == cfg.purpose
                and str(front.get("scale", "")) == cfg.scale
                and existing_axes == cfg.axes
                and str(front.get("fallback", "")) == cfg.fallback
            )
            if same:
                # Same content — no-op (let transactional_rmw return False)
                return existing_text
            raise ConflictError(
                f"_palace.md already exists with different content. "
                f"existing purpose={front.get('purpose')!r}, "
                f"new purpose={cfg.purpose!r}. "
                f"Delete the file first if you want to overwrite."
            )

        # Newly write the file
        front_data: dict[str, Any] = {
            "_kind": "palace_root",
            "_authored_by": "palace_root_init",
            "purpose": cfg.purpose,
            "scale": cfg.scale,
            "axes": list(cfg.axes),
            "fallback": cfg.fallback,
            "created_at": created_at,
        }
        return _fm_render(front_data, "")

    transactional_rmw(p, _mutator)
    return p


def init_palace_root(
    data_dir: Path,
    *,
    purpose: str,
    scale: str,
    axes: tuple[str, ...] | list[str],
    fallback: str,
) -> Path:
    """Convenience helper — validate PalaceRootConfig and call write_palace_root.

    Args:
        data_dir: Database root.
        purpose: One-line purpose of this palace.
        scale: Expected scale (one of 100/1k/10k/100k/1M+).
        axes: Natural classification axes (at least one of
            path/time/topic/person/free).
        fallback: Policy for unfamiliar patterns (auto_create/unsorted).

    Returns:
        Path of the written _palace.md.

    Raises:
        ValueError: On invalid input.
        ConflictError: If a file with different content already exists.
    """
    cfg = PalaceRootConfig(
        purpose=purpose,
        scale=scale,
        axes=tuple(axes),
        fallback=fallback,
    )
    return write_palace_root(data_dir, cfg)


__all__ = [
    "PALACE_ROOT_REL",
    "VALID_AXES",
    "VALID_FALLBACKS",
    "VALID_SCALES",
    "PalaceRootConfig",
    "has_palace_root",
    "init_palace_root",
    "palace_root_path",
    "read_palace_root",
    "write_palace_root",
]

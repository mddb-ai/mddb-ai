from __future__ import annotations

"""Branch overflow detection + ``_attention.md`` signal.

`plans/02-master-plan.md` stage H.3 (option B): MDDB *does not decide*.
When the number of children in a folder exceeds a threshold, write an
``_attention.md`` file to **draw the AI's attention**. Semantic
classification is the calling AI's responsibility.

- Scope: number of *direct child directories* of ``<table>/``,
  ``<table>/<yyyy>/``, ``<yyyy>/<mm>/``, etc. Exclude SSTable directories
  (``sst-*``) and system files (``_*.md``); count only children that
  *look like time buckets*.
- Threshold: default 64 (``branch_overflow_threshold`` config).
- Warning: usage guide written into ``<folder>/_attention.md`` with
  frontmatter.

This module only writes *warnings*. Time-based fallback splitting is
performed by ``BranchTimeFallbackTask`` in :mod:`mddbai.brain.sleep`,
which calls :func:`mddbai.brain.delegation.split_folder`.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mddbai.codec.frontmatter import render as fm_render
from mddbai.core.types import TableName
from mddbai.storage.atomic import atomic_write_text

if TYPE_CHECKING:
    from mddbai.engine import Database


ATTENTION_NAME = "_attention.md"
DEFAULT_THRESHOLD = 64


@dataclass(frozen=True, slots=True)
class OverflowReport:
    """Branch-overflow report for a single folder."""

    rel_path: str
    child_count: int
    threshold: int
    attention_file: Path | None  # Path of the warning file if newly written


def _is_time_bucket_name(name: str) -> bool:
    """Whether the directory name belongs to a time-nesting child:
    4 digits (year), 2 digits (month / day / hour)."""

    return name.isdigit() and len(name) in {2, 4}


def _count_branches(folder: Path) -> int:
    """Number of *time-bucket* child directories of the folder. SSTable,
    system files, and anything else are excluded."""

    if not folder.is_dir():
        return 0
    n = 0
    for entry in folder.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("_") or entry.name.startswith("sst-"):
            continue
        if _is_time_bucket_name(entry.name):
            n += 1
    return n


def _attention_text(folder_rel: str, child_count: int, threshold: int) -> str:
    meta = {
        "_kind": "attention",
        "_authored_by": "stats",
        "rel_path": folder_rel,
        "child_count": child_count,
        "threshold": threshold,
    }
    body = f"""\
# Attention — branch overflow

Folder ``{folder_rel}`` has {child_count} child directories, exceeding
the threshold of {threshold}. The AI must decide one of:

1. **Time-based split** (no semantic classification, mechanical work):
   ```
   mddbai split-folder <data_dir> {folder_rel} --strategy time
   ```
2. **Semantic split** (the AI's real job):
   - Group children by topic into new folders, then re-classify with
     ``mddbai move-record``.
3. **Ignore**:
   - If this file is deleted, the next sleep job may re-create it.
   - To raise the threshold, change
     ``MddbConfig.branch_overflow_threshold``.

This ``_attention.md`` is the *default* written by the sleep job.
Replacing it with one marked ``_authored_by: ai`` makes future sleep
jobs leave it alone.
"""
    return fm_render(meta, body)


def detect_overflow(
    db: Database,
    *,
    threshold: int | None = None,
) -> list[OverflowReport]:
    """Detect branch overflow across every table's folder tree and write
    ``_attention.md``.

    Args:
        db: Target ``Database``.
        threshold: Threshold. ``None`` uses
            ``MddbConfig.branch_overflow_threshold``.

    Returns:
        List of reports for folders that overflowed (folders below the
        threshold are excluded).
    """

    cfg_threshold = getattr(db.config, "branch_overflow_threshold", DEFAULT_THRESHOLD)
    th = int(threshold) if threshold is not None else int(cfg_threshold)
    data_dir = db.config.data_dir
    out: list[OverflowReport] = []
    for table in db.tables():
        table_dir = data_dir / str(table)
        if not table_dir.is_dir():
            continue
        # Folders to check: the table root + time-bucket children (recursive, down to hour)
        stack = [table_dir]
        while stack:
            folder = stack.pop()
            count = _count_branches(folder)
            if count > th:
                rel = folder.relative_to(data_dir).as_posix()
                attention_path = folder / ATTENTION_NAME
                # Preserve any existing _authored_by: ai attention file
                _maybe_write_attention(attention_path, rel, count, th)
                out.append(
                    OverflowReport(
                        rel_path=rel,
                        child_count=count,
                        threshold=th,
                        attention_file=attention_path
                        if attention_path.exists()
                        else None,
                    )
                )
            # Inspect the time-bucket children too
            for entry in folder.iterdir():
                if entry.is_dir() and _is_time_bucket_name(entry.name):
                    stack.append(entry)
    return out


def _maybe_write_attention(
    path: Path, folder_rel: str, child_count: int, threshold: int
) -> None:
    """If the existing file is _authored_by: ai, do not touch it.
    Otherwise overwrite with the stats body."""

    if path.exists():
        try:
            from mddbai.codec.frontmatter import parse as fm_parse  # noqa: PLC0415

            old_meta, _ = fm_parse(path.read_text(encoding="utf-8"))
            if str(old_meta.get("_authored_by", "")) == "ai":
                return
        except Exception:  # noqa: BLE001
            pass
    atomic_write_text(
        path, _attention_text(folder_rel, child_count, threshold), fsync=False
    )


__all__ = [
    "ATTENTION_NAME",
    "DEFAULT_THRESHOLD",
    "OverflowReport",
    "detect_overflow",
]

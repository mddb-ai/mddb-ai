from __future__ import annotations

"""Stage L.9 — HomeostasisTask: automatic absorption of external drawer files.

Role after the switch to the drawer model: re-sync frontmatter of externally
edited drawer .md files via read_drawer + write_drawer. The shard_index /
record model has been removed.

Principles:
- D2 decision — DB does not decide *content*. mtime-based mechanical sync only.
- D3 environment — does not invade user folders. Only frontmatter recomputation.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from mddbai.core.errors import StorageError
from mddbai.core.logging import get_logger
from mddbai.core.types import TableName

if TYPE_CHECKING:
    from mddbai.engine import Database

_log = get_logger("mddbai.brain.homeostasis")


@dataclass
class HomeostasisReport:
    """Homeostasis result for a single table."""

    table: str
    cells_absorbed: int = 0
    cells_removed: int = 0
    cells_reconciled: int = 0
    parse_failures: int = 0
    archived_paths: list[str] = field(default_factory=list)
    gt_dirs_total: int = 0
    gt_dirs_walked: int = 0
    idx_dirs_total: int = 0
    idx_dirs_walked: int = 0


def _absorb_drawer_file(path: Path) -> tuple[int, int]:
    """Absorb a drawer .md — re-sync frontmatter via read_drawer + write_drawer.

    Fix locations where on-disk _content_hash / _sections frontmatter has gone
    stale after an external edit (e.g. via vim). render_drawer inside
    write_drawer recomputes both on every write, so values are unchanged but
    metadata becomes consistent.

    The drawer cache uses mtime-based automatic invalidation, so the next read
    reflects the new values.

    Returns:
        (1, 0) on successful absorption / (0, 1) on read or write failure.
    """
    from mddbai.storage.drawer_store import read_drawer, write_drawer  # noqa: PLC0415

    try:
        contents = read_drawer(path)
    except (OSError, ValueError, TypeError):
        _log.warning("phagocytosis_drawer_parse_fail", path=str(path))
        return 0, 1
    try:
        write_drawer(path, contents)
    except (OSError, StorageError):
        _log.warning("phagocytosis_drawer_write_fail", path=str(path))
        return 0, 1
    return 1, 0


def homeostasis(db: "Database", table: TableName | str) -> HomeostasisReport:
    """Homeostasis for a single table — absorb drawer .md files.

    Walk the _drawers/ folder managed by the drawer engine and re-sync the
    frontmatter of externally edited drawer .md files. The shard index update
    has been removed.
    """
    from mddbai.codec.frontmatter import parse as fm_parse  # noqa: PLC0415

    data_dir = Path(db.config.data_dir)
    table_str = str(table)
    report = HomeostasisReport(table=table_str)

    drawers_dir = data_dir / table_str / "_drawers"
    if not drawers_dir.exists():
        return report

    for md_path in drawers_dir.rglob("*.md"):
        if md_path.name.startswith("_"):
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
            meta, _ = fm_parse(text)
        except (OSError, ValueError, TypeError):
            report.parse_failures += 1
            continue
        if meta.get("_kind") != "drawer":
            continue
        absorbed, failed = _absorb_drawer_file(md_path)
        report.cells_absorbed += absorbed
        report.parse_failures += failed

    return report

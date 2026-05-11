from __future__ import annotations

"""Stage X (2026-05-06) — Related Memory Roads.

Traversal of the ``related`` link graph at drawer or section granularity.
When the AI asks "what's next to this section?", expose adjacent drawers
at hop counts 0/1/2 in a lightweight form.

Recognized storage locations (both):
1. Drawer-level frontmatter ``related: [<table>/<drawer>, ...]``
2. Section-level ``sections_meta[<sid>].related``

Principles:
- D1 no search — no matching, embedding, or spreading. Only follow links
  embedded on disk.
- D2 no decisions — link decisions belong to the AI. The DB only traverses.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from mddbai.codec.frontmatter import parse as fm_parse
from mddbai.codec.section_meta import parse_sections_meta
from mddbai.core.errors import MddbError

if TYPE_CHECKING:
    from mddbai.engine import Database


class RelatedError(MddbError):
    """Related parse / traversal failure."""


@dataclass(slots=True)
class RelatedRef:
    """A single link row."""

    source: str  # Origin drawer (``<table>/<drawer>``)
    target: str  # Destination drawer (``<table>/<drawer>``)
    section_id: str | None = None  # Origin section ID for section-level links
    exists: bool = False  # Whether the drawer actually exists
    kind: str | None = None
    """Typed relation kind (2026-05-09).

    None for legacy untyped ``related`` entries. Set to one of
    VALID_RELATION_KINDS for entries read from ``relations``. AI consumes
    the kind to choose how to traverse — DB does not interpret it.
    """


@dataclass(slots=True)
class RelatedHop:
    drawer: str  # ``<table>/<drawer>``
    distance: int  # 0=self, 1=1 hop, ...
    via: list[str] = field(default_factory=list)  # Path taken to reach it


def _normalize_ref(data_dir: Path, source_table: str, ref: str) -> str | None:
    """Normalize a ``related`` entry to the ``<table>/<drawer>`` form.

    Accepted formats:
    - ``<table>/<drawer>`` (absolute)
    - ``<drawer>`` (relative to the same table)
    """

    ref = ref.strip().lstrip("/")
    if not ref:
        return None
    if ref.endswith(".md"):
        ref = ref[:-3]
    if "/" in ref:
        head = ref.split("/", 1)[0]
        if (data_dir / head).is_dir():
            return ref
    return f"{source_table}/{ref}"


def _drawer_path_for(data_dir: Path, normalized_ref: str) -> Path:
    """Convert ``<table>/<drawer>`` to its on-disk .md path."""

    return data_dir / f"{normalized_ref}.md"


def _read_related_keys(
    path: Path,
) -> tuple[
    list[str],
    dict[str, list[str]],
    dict[str, list[tuple[str, str]]],
]:
    """Extract drawer-level + section-level related keys from a drawer .md.

    Returns:
        ``(drawer_level_related, section_level_related, section_level_typed)``.

        - drawer_level_related: untyped legacy ``related`` at drawer frontmatter.
        - section_level_related: untyped legacy ``related`` per section.
        - section_level_typed: typed ``relations`` per section as
          ``[(target, kind), ...]`` (2026-05-09).
    """

    try:
        text = path.read_text(encoding="utf-8")
        meta, _ = fm_parse(text)
    except (OSError, ValueError):
        return [], {}, {}
    drawer_related: list[str] = []
    raw = meta.get("related")
    if isinstance(raw, list):
        drawer_related = [str(x).strip() for x in raw if isinstance(x, str) and str(x).strip()]
    elif isinstance(raw, str) and raw.strip():
        drawer_related = [raw.strip()]
    section_map: dict[str, list[str]] = {}
    typed_map: dict[str, list[tuple[str, str]]] = {}
    try:
        sections_meta = parse_sections_meta(meta)
    except Exception:  # noqa: BLE001
        sections_meta = {}
    for sid, sm in sections_meta.items():
        if sm.related:
            section_map[sid] = list(sm.related)
        if sm.relations:
            typed_map[sid] = [(r.target, r.kind) for r in sm.relations]
    return drawer_related, section_map, typed_map


def collect_related_refs(
    db: Database, table: str, drawer: str
) -> list[RelatedRef]:
    """Extract all related links of a single drawer (drawer-level + section-level)."""

    data_dir = Path(db.config.data_dir)
    src = f"{table}/{drawer}"
    src_path = _drawer_path_for(data_dir, src)
    if not src_path.exists():
        return []
    drawer_related, section_map, typed_map = _read_related_keys(src_path)
    out: list[RelatedRef] = []
    for ref in drawer_related:
        target = _normalize_ref(data_dir, table, ref)
        if target is None:
            continue
        out.append(
            RelatedRef(
                source=src,
                target=target,
                section_id=None,
                exists=_drawer_path_for(data_dir, target).exists(),
            )
        )
    for sid, refs in section_map.items():
        for ref in refs:
            target = _normalize_ref(data_dir, table, ref)
            if target is None:
                continue
            out.append(
                RelatedRef(
                    source=src,
                    target=target,
                    section_id=sid,
                    exists=_drawer_path_for(data_dir, target).exists(),
                )
            )
    for sid, typed_refs in typed_map.items():
        for ref, kind in typed_refs:
            target = _normalize_ref(data_dir, table, ref)
            if target is None:
                continue
            out.append(
                RelatedRef(
                    source=src,
                    target=target,
                    section_id=sid,
                    exists=_drawer_path_for(data_dir, target).exists(),
                    kind=kind,
                )
            )
    return out


def traverse_related(
    db: Database,
    table: str,
    drawer: str,
    *,
    max_hops: int = 2,
    max_drawers: int = 50,
) -> list[RelatedHop]:
    """BFS traversal — collect adjacent drawers up to ``max_hops`` from the
    starting drawer.

    Cycle / self-link safe. Stops early once ``max_drawers`` is reached
    (explosion guard).

    Returns:
        RelatedHop list sorted by ascending distance, then drawer name.
    """

    if max_hops < 0:
        raise RelatedError(f"max_hops must be >= 0, got {max_hops}")

    data_dir = Path(db.config.data_dir)
    start = f"{table}/{drawer}"
    visited: dict[str, RelatedHop] = {start: RelatedHop(drawer=start, distance=0)}
    frontier: list[tuple[str, list[str]]] = [(start, [])]

    for hop in range(max_hops):
        next_frontier: list[tuple[str, list[str]]] = []
        for cur, path in frontier:
            cur_table, _, cur_drawer = cur.partition("/")
            if not cur_drawer:
                continue
            refs = collect_related_refs(db, cur_table, cur_drawer)
            for ref in refs:
                tgt = ref.target
                if tgt in visited:
                    continue
                # Even links whose drawer does not exist on disk are recorded
                # in `visited`, but they are not traversed further.
                visited[tgt] = RelatedHop(
                    drawer=tgt, distance=hop + 1, via=list(path) + [cur]
                )
                if len(visited) >= max_drawers:
                    break
                if ref.exists:
                    next_frontier.append((tgt, list(path) + [cur]))
            if len(visited) >= max_drawers:
                break
        if len(visited) >= max_drawers:
            break
        frontier = next_frontier
        if not frontier:
            break

    return sorted(
        visited.values(), key=lambda h: (h.distance, h.drawer)
    )


def find_broken_links(db: Database, table: str | None = None) -> list[RelatedRef]:
    """Return only the related links whose target does not exist on disk.

    Args:
        table: Single table to check. None means every table.
    """

    data_dir = Path(db.config.data_dir)
    try:
        tables = [table] if table else [str(t) for t in db.tables()]
    except Exception:  # noqa: BLE001
        tables = []
    out: list[RelatedRef] = []
    for tname in tables:
        if tname is None:
            continue
        table_root = data_dir / tname
        if not table_root.exists():
            continue
        for path in table_root.rglob("*.md"):
            if path.is_dir():
                continue
            rel_parts = path.relative_to(table_root).parts
            if any(part.startswith("_") for part in rel_parts):
                continue
            if path.name.endswith(".lock") or path.name.endswith(".tmp"):
                continue
            rel = path.relative_to(table_root).as_posix()
            if rel.endswith(".md"):
                rel = rel[:-3]
            for ref in collect_related_refs(db, tname, rel):
                if not ref.exists:
                    out.append(ref)
    return out


__all__ = [
    "RelatedError",
    "RelatedHop",
    "RelatedRef",
    "collect_related_refs",
    "find_broken_links",
    "traverse_related",
]

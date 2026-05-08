from __future__ import annotations

"""Stage X (2026-05-06) — Drawer Registry (`_registry/drawers.md`).

Single SSOT for drawer aliases and overlap / drift signals. Lets the AI
check existing drawer candidates within a second *before* creating a new
drawer.

Storage format (data_dir/_registry/drawers.md)::

    ---
    _kind: drawer_registry
    _authored_by: ai
    ---
    # Drawer Registry

    ## aliases

    | alias | canonical |
    |---|---|
    | principle | decisions/principle |
    | identity | decisions/identity |

Registry responsibilities:
- alias -> canonical drawer path mapping (registered by AI)
- Detect drawers where the same stem (final segment) is placed in multiple
  locations (overlap)
- Suggest existing candidates before placing a new drawer (reuse)

Principles:
- D2 no decisions — alias / canonical decisions belong to the AI. The
  registry only manages *space*.
- D7 semantic folders — the registry provides only an alias view; it does
  not move drawers themselves.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from mddbai.codec.frontmatter import parse as fm_parse, render as fm_render
from mddbai.codec.sections import parse_table, render_table
from mddbai.core.errors import MddbError
from mddbai.storage.transactional import transactional_rmw

if TYPE_CHECKING:
    from mddbai.engine import Database


REGISTRY_DIR = "_registry"
REGISTRY_FILENAME = "drawers.md"
REGISTRY_KIND = "drawer_registry"


class RegistryError(MddbError):
    """Drawer registry parse / write failure."""


@dataclass(slots=True)
class DrawerAlias:
    """A single alias row."""

    alias: str
    canonical: str  # ``<table>/<drawer>`` format


@dataclass(slots=True)
class OverlapReport:
    """Signal that the same stem is placed in multiple locations."""

    stem: str
    locations: list[str] = field(default_factory=list)


def registry_path(data_dir: Path) -> Path:
    return Path(data_dir) / REGISTRY_DIR / REGISTRY_FILENAME


def _parse_alias_table(body: str) -> list[DrawerAlias]:
    """Parse the markdown table inside the ## aliases section."""

    if not body.strip():
        return []
    in_section = False
    table_lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## aliases"):
            in_section = True
            continue
        if in_section and stripped.startswith("## "):
            break
        if in_section and stripped.startswith("|"):
            table_lines.append(line)
    if not table_lines or len(table_lines) < 2:
        return []
    try:
        rows = parse_table("\n".join(table_lines))
    except Exception:  # noqa: BLE001
        return []
    out: list[DrawerAlias] = []
    for row in rows:
        a = str(row.get("alias", "")).strip()
        c = str(row.get("canonical", "")).strip()
        if a and c:
            out.append(DrawerAlias(alias=a, canonical=c))
    return out


def _render_registry_body(aliases: list[DrawerAlias]) -> str:
    lines = ["# Drawer Registry", "", "## aliases", ""]
    if aliases:
        rows = [{"alias": a.alias, "canonical": a.canonical} for a in aliases]
        lines.append(render_table(rows, ["alias", "canonical"]).rstrip())
    else:
        lines.append("_(none yet — register via mddbai registry-add)_")
    lines.append("")
    return "\n".join(lines)


def load_aliases(data_dir: Path) -> list[DrawerAlias]:
    """Load the alias list from the registry file. Empty list if missing."""

    p = registry_path(data_dir)
    if not p.exists():
        return []
    try:
        text = p.read_text(encoding="utf-8")
        meta, body = fm_parse(text)
    except (OSError, ValueError):
        return []
    if meta.get("_kind") != REGISTRY_KIND:
        return []
    return _parse_alias_table(body)


def add_alias(data_dir: Path, alias: str, canonical: str) -> Path:
    """Register an alias. If the same alias already exists, update its
    canonical (overwrite).

    ``canonical`` must be in ``<table>/<drawer>`` form (the caller is
    responsible for validation).
    Multi-writer safe — transactional_rmw + FileLock.

    Returns:
        Path of the registry file.
    """

    if not alias or not alias.strip():
        raise RegistryError("alias must be non-empty")
    if not canonical or not canonical.strip():
        raise RegistryError("canonical must be non-empty")
    alias = alias.strip()
    canonical = canonical.strip()

    p = registry_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)

    def _mutator(text: str) -> str:
        if text:
            try:
                meta, body = fm_parse(text)
            except (ValueError,):
                meta, body = {}, ""
            if meta.get("_kind") != REGISTRY_KIND:
                meta = {"_kind": REGISTRY_KIND, "_authored_by": "ai"}
            aliases = _parse_alias_table(body)
        else:
            meta = {"_kind": REGISTRY_KIND, "_authored_by": "ai"}
            aliases = []
        # upsert
        replaced = False
        for i, a in enumerate(aliases):
            if a.alias == alias:
                aliases[i] = DrawerAlias(alias=alias, canonical=canonical)
                replaced = True
                break
        if not replaced:
            aliases.append(DrawerAlias(alias=alias, canonical=canonical))
        aliases.sort(key=lambda a: a.alias)
        return fm_render(meta, _render_registry_body(aliases))

    transactional_rmw(p, _mutator)
    return p


def remove_alias(data_dir: Path, alias: str) -> bool:
    """Remove an alias. Returns True if it existed.

    Multi-writer safe.
    """

    if not alias or not alias.strip():
        return False
    alias = alias.strip()

    p = registry_path(data_dir)
    if not p.exists():
        return False
    removed_flag = [False]

    def _mutator(text: str) -> str:
        if not text:
            return text
        try:
            meta, body = fm_parse(text)
        except ValueError:
            return text
        if meta.get("_kind") != REGISTRY_KIND:
            return text
        aliases = _parse_alias_table(body)
        new_aliases = [a for a in aliases if a.alias != alias]
        if len(new_aliases) == len(aliases):
            return text
        removed_flag[0] = True
        return fm_render(meta, _render_registry_body(new_aliases))

    transactional_rmw(p, _mutator)
    return removed_flag[0]


def resolve_alias(data_dir: Path, alias: str) -> str | None:
    """Resolve alias -> canonical. Returns None if absent."""

    for a in load_aliases(data_dir):
        if a.alias == alias:
            return a.canonical
    return None


def detect_overlaps(db: Database) -> list[OverlapReport]:
    """Walk every table and identify drawers whose stem occurs in multiple
    locations.

    Example: ``decisions/principle/core.md`` + ``notes/core.md`` -> stem
    ``core`` lives in 2 places. A drift signal where the AI cannot tell
    which one to use.
    """

    data_dir = Path(db.config.data_dir)
    by_stem: dict[str, list[str]] = {}
    try:
        tables = db.tables()
    except Exception:  # noqa: BLE001
        tables = []
    for table in tables:
        table_root = data_dir / str(table)
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
            stem = path.stem
            if not stem:
                continue
            full_rel = f"{table}/{path.relative_to(table_root).as_posix()}"
            if full_rel.endswith(".md"):
                full_rel = full_rel[:-3]
            by_stem.setdefault(stem, []).append(full_rel)

    out: list[OverlapReport] = []
    for stem, locs in sorted(by_stem.items()):
        if len(locs) >= 2:
            out.append(OverlapReport(stem=stem, locations=sorted(locs)))
    return out


def suggest_reuse(
    db: Database, intended_drawer: str, *, table: str | None = None
) -> list[str]:
    """Suggest candidates with the same stem / partial match *before*
    creating a new drawer.

    Args:
        intended_drawer: Drawer path to be created (``<sub>/<name>`` or
            ``<name>``).
        table: Table name to check (None means every table).

    Returns:
        List of candidate ``<table>/<drawer>`` paths. Exact matches first,
        then partial matches.
    """

    data_dir = Path(db.config.data_dir)
    intended = intended_drawer.strip("/").lstrip("./")
    if intended.endswith(".md"):
        intended = intended[:-3]
    if not intended:
        return []
    stem = intended.split("/")[-1]

    try:
        tables = [str(table)] if table else [str(t) for t in db.tables()]
    except Exception:  # noqa: BLE001
        tables = []

    exact: list[str] = []
    partial: list[str] = []
    for tname in tables:
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
            cand_stem = path.stem
            cand_rel = path.relative_to(table_root).as_posix()
            if cand_rel.endswith(".md"):
                cand_rel = cand_rel[:-3]
            full = f"{tname}/{cand_rel}"
            if cand_stem == stem:
                exact.append(full)
            elif stem and stem in cand_stem:
                partial.append(full)

    return sorted(exact) + sorted(partial)


__all__ = [
    "REGISTRY_DIR",
    "REGISTRY_FILENAME",
    "REGISTRY_KIND",
    "DrawerAlias",
    "OverlapReport",
    "RegistryError",
    "add_alias",
    "detect_overlaps",
    "load_aliases",
    "registry_path",
    "remove_alias",
    "resolve_alias",
    "suggest_reuse",
]

from __future__ import annotations

"""Stage X (2026-05-06) — Navigate API.

Take a single natural-language cue and propose *route candidates* only.
No search engine, embeddings, or hidden indexes. drawer bodies are not
read — only the cue traces already on disk (palace, the table
``_summary.md``, drawer filenames, sections_meta.cue / aliases /
importance / memory_zone / related, registry aliases) are inspected in a
lightweight form.

Principles:
- D1 no search — substring / token intersection is *space work*
  (consistent with ``responsibility-split.md`` D2: same category as
  "regex phrase extraction -> insert lexicon node"). No semantic decisions.
- D2 no decisions — the score is an *internal ranking value*; only
  ``reason`` and ``signals`` are exposed externally. The AI makes the
  final call from the route candidates.
- D7 Loci — the *semantic slug* of folders / drawers / sections is the
  cue. Traces that the AI placed itself.
- V3 byte — zero bytes of drawer body are read. Only frontmatter (which
  may be served by the cache). The per-table ``_summary.md`` is read
  once with a ~4 KB cap (route hint, with an explosion guard) and is
  unrelated to drawer bodies.

Lookup order:
1. palace context (if present, only the axes / purpose are used as hints)
2. table candidates — match on table name / _summary.md body
3. drawer candidates — match on drawer rel path segments + registry alias boost
4. section candidates — match on section_id + sections_meta
5. synthesize route candidates (capped by max_routes)
6. fallback — if every score is 0, return "No confident route found"

Explosion guards:
- max_tables / max_drawers / max_sections caps
- zero drawer body reads
- one ``_summary.md`` read per table (~4 KB cap)
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mddbai.brain.registry import load_aliases
from mddbai.brain.palace_root import has_palace_root, read_palace_root
from mddbai.codec.frontmatter import parse as fm_parse
from mddbai.codec.section_meta import parse_sections_meta
from mddbai.core.errors import MddbError

if TYPE_CHECKING:
    from mddbai.engine import Database


SUMMARY_NAME = "_summary.md"
SUMMARY_READ_BYTES_CAP = 4096


# ---- defaults (shared by CLI / API) ------------------------------------

DEFAULT_MAX_ROUTES = 5
DEFAULT_MAX_TABLES = 5
DEFAULT_MAX_DRAWERS = 20
DEFAULT_MAX_SECTIONS = 50


class NavigateError(MddbError):
    """Navigate failure (invalid input / IO)."""


@dataclass(slots=True)
class NavigateRoute:
    """A single route candidate. ``table`` is always populated.

    If ``drawer`` is None it is a table-level hint; if ``section`` is None
    it is a drawer-level hint. When all fields are populated it points to
    a precise section.
    """

    table: str
    drawer: str | None
    section: str | None
    reason: str
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "drawer": self.drawer,
            "section": self.section,
            "reason": self.reason,
            "signals": list(self.signals),
        }


@dataclass(slots=True)
class NavigateResult:
    """Navigate output — route candidates + warnings."""

    cue: str
    routes: list[NavigateRoute] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cue": self.cue,
            "routes": [r.to_dict() for r in self.routes],
            "warnings": list(self.warnings),
        }


# ---- tokenization -------------------------------------------------------

_TOKEN_SPLIT = re.compile(r"[\s/_\-.,;:!?'\"\(\)\[\]\{\}<>|]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Split a cue / identifier into small tokens. Korean is preserved at
    whitespace boundaries.

    Lowercase, then split on punctuation / whitespace / ``/`` / ``_`` /
    ``-``. Drop empty tokens and deduplicate (preserving order of first
    occurrence).
    """

    if not text:
        return []
    lowered = text.lower()
    raw = _TOKEN_SPLIT.split(lowered)
    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        t = t.strip()
        if not t:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _token_overlap(cue_tokens: Iterable[str], target: str) -> tuple[int, list[str]]:
    """Number of cue tokens that occur in ``target`` and the list of
    matched tokens.

    Substring match. Very short cue tokens (single character) are too
    noisy, so only *exact-token* matches are counted for them.
    """

    if not target:
        return 0, []
    target_lower = target.lower()
    target_tokens = set(tokenize(target_lower))
    matched: list[str] = []
    for t in cue_tokens:
        if not t:
            continue
        if len(t) == 1:
            if t in target_tokens:
                matched.append(t)
            continue
        if t in target_lower:
            matched.append(t)
    return len(matched), matched


# ---- internal scoring datatypes ----------------------------------------


@dataclass(slots=True)
class _TableScore:
    table: str
    score: float
    signals: set[str] = field(default_factory=set)
    reason_bits: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _DrawerScore:
    table: str
    drawer: str  # Path relative to the table (no .md suffix)
    score: float
    signals: set[str] = field(default_factory=set)
    reason_bits: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _SectionScore:
    table: str
    drawer: str
    section: str
    score: float
    signals: set[str] = field(default_factory=set)
    reason_bits: list[str] = field(default_factory=list)


# ---- table candidate scoring -------------------------------------------


def _read_summary_text(path: Path) -> str:
    """Read only a slice (4 KB cap) of the ``_summary.md`` body. Frontmatter
    is ignored.

    Returns the empty string if the file is missing or unreadable.
    """

    if not path.exists() or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > SUMMARY_READ_BYTES_CAP * 4:
        text = text[: SUMMARY_READ_BYTES_CAP * 4]
    try:
        _, body = fm_parse(text)
    except Exception:  # noqa: BLE001
        body = text
    if len(body) > SUMMARY_READ_BYTES_CAP:
        body = body[:SUMMARY_READ_BYTES_CAP]
    return body


def _score_tables(
    db: Database, cue_tokens: list[str], *, max_tables: int
) -> list[_TableScore]:
    """Score every table and return the top ``max_tables``.

    Even tables with zero cue matches are included up to the cap so route
    hints are never completely empty (alphabetical fill).
    """

    data_dir = Path(db.config.data_dir)
    try:
        tables = [str(t) for t in db.tables()]
    except Exception:  # noqa: BLE001
        tables = []

    scored: list[_TableScore] = []
    for tname in tables:
        ts = _TableScore(table=tname, score=0.0)
        n_name, name_hits = _token_overlap(cue_tokens, tname)
        if n_name:
            ts.score += 2.0 * n_name
            ts.signals.add("table_name")
            ts.reason_bits.append(f"table name match: {','.join(name_hits)}")

        sum_path = data_dir / tname / SUMMARY_NAME
        if sum_path.exists():
            body = _read_summary_text(sum_path)
            if body:
                n_sum, sum_hits = _token_overlap(cue_tokens, body)
                if n_sum:
                    ts.score += 1.0 * n_sum
                    ts.signals.add("table_summary")
                    ts.reason_bits.append(
                        f"summary match: {','.join(sum_hits[:3])}"
                    )
        scored.append(ts)

    # Fill up to max_tables even with zero scores so the candidate space is preserved
    scored.sort(key=lambda s: (-s.score, s.table))
    return scored[: max(1, max_tables)]


# ---- drawer candidate scoring ------------------------------------------


def _list_table_drawers(
    db: Database, table: str, *, max_drawers: int
) -> list[str]:
    """List drawers in the table (depth=0). The DB auto-clamps if a flat
    dump would explode."""

    try:
        entries = db.list_drawers(table, depth=0)
    except Exception:  # noqa: BLE001
        return []
    # entries may mix leaves (drawer rel) and folders ("name/")
    leaves = [e for e in entries if not e.endswith("/")]
    if len(leaves) > max_drawers * 5:
        # Explosion guard: for very large tables, quickly truncate leaves
        # that do not match the cue. ``leaves`` is already sorted.
        leaves = leaves[: max_drawers * 5]
    return leaves


def _score_drawers(
    db: Database,
    cue_tokens: list[str],
    table_scored: list[_TableScore],
    *,
    max_drawers: int,
    alias_canonical: dict[str, str],
) -> list[_DrawerScore]:
    """Score drawer candidates for each table. Take the top ``max_drawers``
    across all tables combined."""

    out: list[_DrawerScore] = []
    aliases_by_token = {a.lower(): canon for a, canon in alias_canonical.items()}

    for ts in table_scored:
        leaves = _list_table_drawers(db, ts.table, max_drawers=max_drawers)
        for rel in leaves:
            ds = _DrawerScore(table=ts.table, drawer=rel, score=0.0)
            # Some of the table score leaks in — when the table aligns with
            # the cue, drawers inside it get a weak boost.
            if ts.score > 0:
                ds.score += min(1.0, ts.score * 0.25)
                if "table_name" in ts.signals:
                    ds.signals.add("table_name")
                if "table_summary" in ts.signals:
                    ds.signals.add("table_summary")

            # Match against drawer rel path segments
            n_path, path_hits = _token_overlap(cue_tokens, rel.replace("/", " "))
            if n_path:
                ds.score += 3.0 * n_path
                ds.signals.add("drawer_name")
                ds.reason_bits.append(
                    f"drawer name match: {','.join(path_hits[:3])}"
                )

            # Exact alias hit — a large boost when one of the cue tokens
            # is an alias whose canonical points to this drawer.
            for tok in cue_tokens:
                canon = aliases_by_token.get(tok)
                if canon and canon == f"{ts.table}/{rel}":
                    ds.score += 5.0
                    ds.signals.add("alias")
                    ds.reason_bits.append(f"alias match: {tok}")
                    break

            out.append(ds)

    out.sort(key=lambda d: (-d.score, d.table, d.drawer))
    return out[: max(1, max_drawers)]


# ---- section candidate scoring -----------------------------------------


def _read_drawer_sections_meta(
    db: Database, table: str, drawer: str
) -> tuple[list[str], dict[str, Any]]:
    """Return the drawer's list of section_ids plus its sections_meta dict.

    Does not read the drawer body — only frontmatter served from the cache.
    """

    try:
        sections = db.list_sections(table, drawer)
    except Exception:  # noqa: BLE001
        sections = []
    fm: dict[str, Any] = {}
    try:
        path = db._drawer_path(table, drawer)  # type: ignore[attr-defined]
        meta = db.drawer_engine.cache.get_meta(path)
        if isinstance(meta, dict):
            fm = dict(meta)
    except Exception:  # noqa: BLE001
        fm = {}
    return sections, fm


def _score_sections(
    db: Database,
    cue_tokens: list[str],
    drawer_scored: list[_DrawerScore],
    *,
    max_sections: int,
) -> list[_SectionScore]:
    """Score sections for each drawer candidate. Take the top
    ``max_sections`` overall."""

    out: list[_SectionScore] = []
    for ds in drawer_scored:
        sections, fm = _read_drawer_sections_meta(db, ds.table, ds.drawer)
        try:
            sm_map = parse_sections_meta(fm)
        except Exception:  # noqa: BLE001
            sm_map = {}

        for sid in sections:
            sc = _SectionScore(
                table=ds.table, drawer=ds.drawer, section=sid, score=0.0
            )
            # Pass some of the drawer score down to its sections
            if ds.score > 0:
                sc.score += min(1.5, ds.score * 0.4)
                for s in ds.signals:
                    sc.signals.add(s)
                if ds.reason_bits:
                    sc.reason_bits.extend(ds.reason_bits[:1])

            # section id match
            n_id, id_hits = _token_overlap(cue_tokens, sid.replace("/", " "))
            if n_id:
                sc.score += 4.0 * n_id
                sc.signals.add("section_id")
                sc.reason_bits.append(
                    f"section id match: {','.join(id_hits[:3])}"
                )

            # section metadata
            sm = sm_map.get(sid)
            if sm is not None:
                if sm.cue:
                    cue_text = " ".join(sm.cue)
                    n_cue, cue_hits = _token_overlap(cue_tokens, cue_text)
                    if n_cue:
                        sc.score += 3.0 * n_cue
                        sc.signals.add("section_cue")
                        sc.reason_bits.append(
                            f"section cue match: {','.join(cue_hits[:3])}"
                        )
                # section alias — Korean <-> English <-> abbreviation
                # bridge (set via ``--alias`` at write time). Same weight
                # as section_cue. User decision 2026-05-08 — the trace
                # promised by the write skill should also act as a bridge
                # during recall.
                if sm.aliases:
                    alias_text = " ".join(sm.aliases)
                    n_alias, alias_hits = _token_overlap(cue_tokens, alias_text)
                    if n_alias:
                        sc.score += 3.0 * n_alias
                        sc.signals.add("section_alias")
                        sc.reason_bits.append(
                            f"section alias match: {','.join(alias_hits[:3])}"
                        )
                if sm.importance is not None and sm.importance > 0:
                    sc.score += float(sm.importance)
                    sc.signals.add("importance")
                if sm.memory_zone == "hot":
                    sc.score += 0.6
                    sc.signals.add("memory_zone")
                elif sm.memory_zone == "warm":
                    sc.score += 0.2
                    sc.signals.add("memory_zone")
                if sm.related:
                    sc.score += min(0.5, 0.1 * len(sm.related))
                    sc.signals.add("related")

            out.append(sc)

    out.sort(
        key=lambda s: (-s.score, s.table, s.drawer, s.section)
    )
    return out[: max(1, max_sections)]


# ---- route synthesis ----------------------------------------------------


def _build_reason(bits: list[str]) -> str:
    if not bits:
        return "no specific signal"
    # de-dup keeping order
    seen: set[str] = set()
    uniq: list[str] = []
    for b in bits:
        if b in seen:
            continue
        seen.add(b)
        uniq.append(b)
    return "; ".join(uniq[:3])


def _has_strong_signal(signals: set[str]) -> bool:
    """matching that came from cue (not just transferred from table-level)."""

    return bool(
        signals & {"section_id", "section_cue", "section_alias", "drawer_name", "alias"}
    )


def navigate(
    db: Database,
    cue: str,
    *,
    max_routes: int = DEFAULT_MAX_ROUTES,
    max_tables: int = DEFAULT_MAX_TABLES,
    max_drawers: int = DEFAULT_MAX_DRAWERS,
    max_sections: int = DEFAULT_MAX_SECTIONS,
    fallback_disabled: bool = False,
) -> NavigateResult:
    """Propose up to ``max_routes`` route candidates for a cue.

    Args:
        db: Open Database.
        cue: A single line of natural-language cue (Korean / English /
            anything).
        max_routes: Maximum number of routes to return.
        max_tables: Internal cap on table candidates (prevents downstream
            explosion).
        max_drawers: Internal cap on drawer candidates.
        max_sections: Internal cap on section candidates.
        fallback_disabled: If True, only the primary navigation pass
            (palace / table summary / drawer frontmatter / sections_meta)
            is used. The drawer-level and palace-purpose fallbacks are
            blocked, so only *precise section candidates* are exposed. If
            the cue cannot reach the section stage, returns empty routes
            with a warning. Used for strict acceptance / harness tests.

    Returns:
        NavigateResult — empty ``routes`` signals "no confident route".

    Raises:
        NavigateError: On invalid input.
    """

    if cue is None:
        raise NavigateError("cue must not be None")
    cue = cue.strip()
    if not cue:
        raise NavigateError("cue must not be empty")
    if max_routes <= 0:
        raise NavigateError(f"max_routes must be >= 1, got {max_routes}")

    cue_tokens = tokenize(cue)
    result = NavigateResult(cue=cue)

    if not cue_tokens:
        result.warnings.append(
            "cue is empty after tokenization — provide a more specific cue"
        )
        return result

    data_dir = Path(db.config.data_dir)

    # palace context (low influence; used only as a hint)
    palace_purpose: str | None = None
    if has_palace_root(data_dir):
        try:
            cfg = read_palace_root(data_dir)
        except Exception:  # noqa: BLE001
            cfg = None
        if cfg is not None:
            palace_purpose = cfg.purpose

    # registry alias list
    alias_canonical: dict[str, str] = {}
    try:
        for a in load_aliases(data_dir):
            alias_canonical[a.alias] = a.canonical
    except Exception:  # noqa: BLE001
        pass

    # 1. table candidates
    table_scored = _score_tables(db, cue_tokens, max_tables=max_tables)
    if not table_scored:
        result.warnings.append(
            "0 tables — run `mddbai init` or `put-section` first"
        )
        return result

    # 2. drawer candidates
    drawer_scored = _score_drawers(
        db,
        cue_tokens,
        table_scored,
        max_drawers=max_drawers,
        alias_canonical=alias_canonical,
    )

    # 3. section candidates
    section_scored = _score_sections(
        db, cue_tokens, drawer_scored, max_sections=max_sections
    )

    # 4. Synthesize routes — strong signals first, then drawer / table
    #    fallback if needed.
    routes: list[NavigateRoute] = []
    seen_keys: set[tuple[str, str | None, str | None]] = set()

    for sc in section_scored:
        if sc.score <= 0 or not _has_strong_signal(sc.signals):
            continue
        key = (sc.table, sc.drawer, sc.section)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        routes.append(
            NavigateRoute(
                table=sc.table,
                drawer=sc.drawer,
                section=sc.section,
                reason=_build_reason(sc.reason_bits),
                signals=sorted(sc.signals),
            )
        )
        if len(routes) >= max_routes:
            break

    if not fallback_disabled and len(routes) < max_routes:
        for ds in drawer_scored:
            if ds.score <= 0 or not _has_strong_signal(ds.signals):
                continue
            key = (ds.table, ds.drawer, None)
            if key in seen_keys:
                continue
            # If a section-level route already exists for this drawer, skip the drawer-level entry
            if any(r.table == ds.table and r.drawer == ds.drawer for r in routes):
                continue
            seen_keys.add(key)
            routes.append(
                NavigateRoute(
                    table=ds.table,
                    drawer=ds.drawer,
                    section=None,
                    reason=_build_reason(ds.reason_bits),
                    signals=sorted(ds.signals),
                )
            )
            if len(routes) >= max_routes:
                break

    if not fallback_disabled and not routes and palace_purpose:
        n_purpose, _ = _token_overlap(cue_tokens, palace_purpose)
        if n_purpose and table_scored:
            top = table_scored[0]
            routes.append(
                NavigateRoute(
                    table=top.table,
                    drawer=None,
                    section=None,
                    reason=f"palace purpose match: {palace_purpose[:60]}",
                    signals=["palace"],
                )
            )

    if not routes:
        if fallback_disabled:
            result.warnings.append(
                "No confident section-level route found (fallback_disabled)."
            )
        else:
            result.warnings.append("No confident route found.")

    result.routes = routes
    return result


__all__ = [
    "DEFAULT_MAX_DRAWERS",
    "DEFAULT_MAX_ROUTES",
    "DEFAULT_MAX_SECTIONS",
    "DEFAULT_MAX_TABLES",
    "NavigateError",
    "NavigateResult",
    "NavigateRoute",
    "navigate",
    "tokenize",
]

from __future__ import annotations

"""K.3 / K.4 — Disk IO for the user lexicon graph.

``data/_user_lexicon/`` is *not an LSM table* but a *meta folder* (like
``_summary.md`` and ``_links.md``). It is read-mostly data that does not
need LSM transactions or compaction.

Layout:

::

    data/
      _user_lexicon/
        _signals.md            # signal-word dictionary
                                 (lexicon_extract.ensure_signals_dictionary)
        _index.md              # surface -> node_path matrix (AI's entry point)
        nodes/
          <surface_hash>.md    # one file per phrase (frontmatter + body)
      _user_lexicon_links.md   # typed edges (kind, source, target, weight)

Five edge kinds (K.4):

- ``said_in_turn_with`` — utterance <-> artifact (automatic, sleep)
- ``aliased_to``        — user expression -> AI standard expression
- ``referenced_by``     — deictic -> immediately preceding artifact
- ``confirmed_with``    — artifact following a user confirmation signal
- ``rejected_against``  — artifact following a user rejection signal
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from mddbai.codec.frontmatter import parse as _fm_parse
from mddbai.codec.frontmatter import render as _fm_render
from mddbai.storage.atomic import atomic_write_text
from mddbai.storage.locks import FileLock
from mddbai.storage.transactional import transactional_rmw

# ---------------------------------------------------------------------------
# Edge kind (K.4)
# ---------------------------------------------------------------------------

EDGE_KINDS: tuple[str, ...] = (
    "said_in_turn_with",
    "aliased_to",
    "referenced_by",
    "confirmed_with",
    "rejected_against",
)

EdgeKind = Literal[
    "said_in_turn_with",
    "aliased_to",
    "referenced_by",
    "confirmed_with",
    "rejected_against",
]


@dataclass(frozen=True, slots=True)
class LexiconEdge:
    """A typed edge — one line = one edge."""

    kind: str
    source: str  # Usually a phrase node id (surface_hash) or an utterance id
    target: str  # An artifact record id, or another phrase node id
    weight: float = 1.0
    session_id: str | None = None
    ts_iso: str | None = None


# ---------------------------------------------------------------------------
# Phrase node key (surface normalization + short sha1 hash)
# ---------------------------------------------------------------------------


def _normalize(surface: str) -> str:
    """Case-insensitive + collapse internal whitespace + trim."""

    return re.sub(r"\s+", " ", surface).strip().lower()


def phrase_key(surface: str) -> str:
    """surface -> a short collision-avoiding hash key (12 chars)."""

    norm = _normalize(surface)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Phrase node IO
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PhraseNode:
    """Phrase node — frontmatter holds graph meta, body holds the human description."""

    surface: str
    normalized: str
    first_seen_ns: int
    last_seen_ns: int
    seen_count: int
    session_ids: list[str]
    intent_tags: list[str]
    strength: float
    edges: list[LexiconEdge] = field(default_factory=list)

    @property
    def key(self) -> str:
        return phrase_key(self.surface)

    def to_frontmatter(self) -> dict[str, object]:
        return {
            "id": self.key,
            "type": "phrase",
            "surface": self.surface,
            "normalized": self.normalized,
            "first_seen": _ns_iso(self.first_seen_ns),
            "first_seen_ns": self.first_seen_ns,
            "last_seen": _ns_iso(self.last_seen_ns),
            "last_seen_ns": self.last_seen_ns,
            "seen_count": self.seen_count,
            "session_ids": list(self.session_ids),
            "intent_tags": list(self.intent_tags),
            "strength": round(float(self.strength), 4),
        }


def _ns_iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=UTC).isoformat()


# Split user-utterance lexicon and AI-response lexicon into two locations.
# Automatic = parse + write (space work, D2-aligned). Matching = done by
# the AI itself at recall time.
LEXICON_SPACES: tuple[str, ...] = ("user", "ai")
_SPACE_DIR_NAME: dict[str, str] = {
    "user": "_user_lexicon",
    "ai": "_ai_lexicon",
}


def lexicon_dir(data_dir: Path, *, space: str = "user") -> Path:
    """Disk root of a lexicon space.

    Args:
        data_dir: ``.mddbai/`` palace location.
        space: ``user`` (user utterances, default) or ``ai`` (AI responses).
    """

    if space not in LEXICON_SPACES:
        raise ValueError(f"unknown lexicon space: {space!r}")
    return data_dir / _SPACE_DIR_NAME[space]


# ---------------------------------------------------------------------------
# Q.1 — Two-tier layout (episodic / semantic) + legacy compatibility
# ---------------------------------------------------------------------------

EPISODIC_DIR = "episodic"
SEMANTIC_DIR = "semantic"
LEGACY_NODES_DIR = "nodes"

# Lookup order: stable anchors (semantic) -> fast-changing (episodic) -> legacy
_TIER_ORDER: tuple[str, ...] = ("semantic", "episodic", "legacy")


def episodic_dir(data_dir: Path, *, space: str = "user") -> Path:
    """Q.1 — Fast-changing phrase nodes. mtime-based fast-decay region."""

    return lexicon_dir(data_dir, space=space) / EPISODIC_DIR


def semantic_dir(data_dir: Path, *, space: str = "user") -> Path:
    """Q.1 — Promoted anchor nodes. Protected from automatic cleanup."""

    return lexicon_dir(data_dir, space=space) / SEMANTIC_DIR


def nodes_dir(data_dir: Path, *, space: str = "user") -> Path:
    """Legacy ``<lexicon>/nodes/`` — nodes written before Q.1.
    Read-compat only."""

    return lexicon_dir(data_dir, space=space) / LEGACY_NODES_DIR


def _tier_dir(data_dir: Path, tier: str, *, space: str = "user") -> Path:
    if tier == "semantic":
        return semantic_dir(data_dir, space=space)
    if tier == "episodic":
        return episodic_dir(data_dir, space=space)
    if tier == "legacy":
        return nodes_dir(data_dir, space=space)
    raise ValueError(f"unknown lexicon tier: {tier!r}")


def node_path_in(
    data_dir: Path, surface: str, *, tier: str, space: str = "user"
) -> Path:
    """Node path with explicit tier. tier is in {'semantic', 'episodic', 'legacy'}."""

    return _tier_dir(data_dir, tier, space=space) / f"{phrase_key(surface)}.md"


def node_path(data_dir: Path, surface: str, *, space: str = "user") -> Path:
    """*Default* node path (episodic). After Q.1 new nodes are written
    to episodic.

    For compatibility: this is the *predictable location* external callers
    expect. Actual reads via ``find_node_path`` or ``load_node`` look across
    all three tiers.
    """

    return node_path_in(data_dir, surface, tier="episodic", space=space)


def find_node_path(
    data_dir: Path, surface: str, *, space: str = "user"
) -> tuple[str, Path] | None:
    """The tier and path of the stored node. ``None`` if absent.

    Lookup order: ``semantic -> episodic -> legacy``. Returns the first hit.
    """

    fname = f"{phrase_key(surface)}.md"
    for tier in _TIER_ORDER:
        cand = _tier_dir(data_dir, tier, space=space) / fname
        if cand.exists():
            return (tier, cand)
    return None


def iter_all_nodes(
    data_dir: Path, *, space: str = "user"
) -> list[tuple[str, Path]]:
    """List of ``(tier, path)`` for node files across every tier.

    Preserves the intent of callers (refresh_index, tests, etc.) — it
    safely extends the original ``nodes_dir`` scan to a *3-tier union*.
    """

    out: list[tuple[str, Path]] = []
    for tier in _TIER_ORDER:
        d = _tier_dir(data_dir, tier, space=space)
        if not d.is_dir():
            continue
        for p in d.glob("*.md"):
            out.append((tier, p))
    return out


def load_node(
    data_dir: Path, surface: str, *, space: str = "user"
) -> PhraseNode | None:
    """Load the node after a 3-tier lookup. ``None`` if absent."""

    found = find_node_path(data_dir, surface, space=space)
    if found is None:
        return None
    _tier, path = found
    fm, _ = _fm_parse(path.read_text(encoding="utf-8"))
    if fm.get("type") != "phrase":
        return None
    surf = str(fm.get("surface", surface))
    return PhraseNode(
        surface=surf,
        normalized=str(fm.get("normalized", _normalize(surf))),
        first_seen_ns=int(fm.get("first_seen_ns", 0)),
        last_seen_ns=int(fm.get("last_seen_ns", 0)),
        seen_count=int(fm.get("seen_count", 0)),
        session_ids=[str(s) for s in fm.get("session_ids", []) if isinstance(s, (str, int))],
        intent_tags=[str(t) for t in fm.get("intent_tags", []) if isinstance(t, (str, int))],
        strength=float(fm.get("strength", 0.30)),
    )


def save_node(
    data_dir: Path,
    node: PhraseNode,
    *,
    tier: str = "episodic",
    space: str = "user",
) -> Path:
    """Save the node. ``tier`` defaults to ``episodic`` (Q.1's default
    location for new nodes).

    To save an existing node back into its *current tier*, use
    ``find_node_path`` to detect it and pass the tier explicitly.
    """

    path = node_path_in(data_dir, node.surface, tier=tier, space=space)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f'# "{node.surface}"\n\n'
        f"- normalized: `{node.normalized}`\n"
        f"- seen_count: {node.seen_count}\n"
        f"- strength: {node.strength:.2f}\n"
        f"- first_seen: {_ns_iso(node.first_seen_ns)}\n"
        f"- last_seen: {_ns_iso(node.last_seen_ns)}\n"
        f"- tier: {tier}\n"
    )
    if node.intent_tags:
        body += f"- intent_tags: {', '.join(node.intent_tags)}\n"
    body += "\n<!-- The body may be filled in by the sleep job via LLM delegation. -->\n"
    atomic_write_text(path, _fm_render(node.to_frontmatter(), body))
    return path


def promote_to_semantic(
    data_dir: Path, surface: str, *, space: str = "user"
) -> Path | None:
    """Q.1.3 — Promote an episodic node to the semantic tier.

    Returns ``None`` if it is already semantic, or if the node does not exist.

    Implementation: if the current tier is episodic or legacy, save the
    node again at the semantic path and then unlink the previous tier's
    file. The order is *write first, unlink after* to keep atomicity.
    """

    found = find_node_path(data_dir, surface, space=space)
    if found is None:
        return None
    cur_tier, cur_path = found
    if cur_tier == "semantic":
        return None
    node = load_node(data_dir, surface, space=space)
    if node is None:
        return None
    new_path = save_node(data_dir, node, tier="semantic", space=space)
    # Remove leftovers from the previous tier (unless it is the same file as semantic)
    if cur_path != new_path:
        try:
            cur_path.unlink()
        except OSError:
            pass
    return new_path


def upsert_phrase(
    data_dir: Path,
    surface: str,
    *,
    ts_ns: int,
    session_id: str,
    intent_tags: list[str] | None = None,
    strength_increment: float = 0.10,
    initial_strength: float = 0.30,
    max_strength: float = 1.0,
    space: str = "user",
) -> PhraseNode:
    """Insert a phrase as a node (if absent), or strengthen the existing
    node (if present).

    - ``seen_count`` += 1
    - ``last_seen_ns`` = ts_ns
    - ``strength`` += strength_increment (capped at max_strength)
    - Append session_id to ``session_ids`` (no duplicates)
    - Union of ``intent_tags``

    Stage Z.9 (2026-05-03) — multi-process safe:
    take a FileLock on the node path of the same surface and serialize
    read-modify-write. Other surfaces remain parallel.
    """

    intent = list(intent_tags or [])

    # Pick the lock path — the tier path of an existing node, otherwise the episodic path
    found = find_node_path(data_dir, surface, space=space)
    lock_path = (
        found[1]
        if found is not None
        else node_path_in(data_dir, surface, tier="episodic", space=space)
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with FileLock(lock_path):
        # Verify the on-disk state inside the lock — another process may have written in between
        existing = load_node(data_dir, surface, space=space)

        if existing is None:
            node = PhraseNode(
                surface=surface,
                normalized=_normalize(surface),
                first_seen_ns=ts_ns,
                last_seen_ns=ts_ns,
                seen_count=1,
                session_ids=[session_id],
                intent_tags=intent,
                strength=initial_strength,
            )
            # Q.1: new nodes go into the episodic tier (default)
            save_node(data_dir, node, space=space)
            return node

        sids = list(existing.session_ids)
        if session_id not in sids:
            sids.append(session_id)
        tags = sorted(set(existing.intent_tags) | set(intent))
        new_node = PhraseNode(
            surface=existing.surface,
            normalized=existing.normalized,
            first_seen_ns=existing.first_seen_ns,
            last_seen_ns=max(existing.last_seen_ns, ts_ns),
            seen_count=existing.seen_count + 1,
            session_ids=sids,
            intent_tags=tags,
            strength=min(max_strength, existing.strength + strength_increment),
        )
        # Q.1: keep the *current tier* of an existing node (a node already
        # promoted to semantic must not be demoted back to episodic).
        # External promote helpers handle explicit changes.
        found_inner = find_node_path(data_dir, surface, space=space)
        current_tier = found_inner[0] if found_inner is not None else "episodic"
        save_node(data_dir, new_node, tier=current_tier, space=space)
        return new_node


# ---------------------------------------------------------------------------
# Edges (typed) — single file _user_lexicon_links.md (JSONL body)
# ---------------------------------------------------------------------------

_EDGES_FRONTMATTER: dict[str, object] = {
    "type": "lexicon_links",
    "format": "jsonl",
    "kinds": list(EDGE_KINDS),
    "_authored_by": "stats",
}


def edges_path(data_dir: Path) -> Path:
    return data_dir / "_user_lexicon_links.md"


def _edge_to_jsonl(edge: LexiconEdge) -> str:
    obj: dict[str, object] = {
        "kind": edge.kind,
        "source": edge.source,
        "target": edge.target,
        "weight": round(float(edge.weight), 4),
    }
    if edge.session_id:
        obj["session_id"] = edge.session_id
    if edge.ts_iso:
        obj["ts"] = edge.ts_iso
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _jsonl_to_edge(line: str) -> LexiconEdge | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    kind = obj.get("kind")
    src = obj.get("source")
    tgt = obj.get("target")
    if not isinstance(kind, str) or not isinstance(src, str) or not isinstance(tgt, str):
        return None
    if kind not in EDGE_KINDS:
        return None
    weight = obj.get("weight", 1.0)
    return LexiconEdge(
        kind=kind,
        source=src,
        target=tgt,
        weight=float(weight) if isinstance(weight, (int, float)) else 1.0,
        session_id=obj.get("session_id") if isinstance(obj.get("session_id"), str) else None,
        ts_iso=obj.get("ts") if isinstance(obj.get("ts"), str) else None,
    )


def _parse_edges_from_text(text: str) -> list[LexiconEdge]:
    """Parse the text of edges.md into a list of LexiconEdge.
    Helper used by the multi-process-safe path.

    Returns an empty list for empty text.
    """

    if not text:
        return []
    _, body = _fm_parse(text)
    out: list[LexiconEdge] = []
    in_block = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_block = not in_block
            continue
        if not in_block or not stripped:
            continue
        edge = _jsonl_to_edge(stripped)
        if edge is not None:
            out.append(edge)
    return out


def _render_edges_text(edges: list[LexiconEdge]) -> str:
    """Serialize a list of LexiconEdge into edges.md format text."""

    body_lines = [
        "# Lexicon Edges (typed graph)",
        "",
        f"> Edge kinds: {', '.join(EDGE_KINDS)}",
        "> Each line is one typed edge (JSONL).",
        "",
        "```jsonl",
    ]
    body_lines.extend(_edge_to_jsonl(e) for e in edges)
    body_lines.append("```")
    body_lines.append("")
    return _fm_render(_EDGES_FRONTMATTER, "\n".join(body_lines))


def load_edges(data_dir: Path) -> list[LexiconEdge]:
    path = edges_path(data_dir)
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return _parse_edges_from_text(text)


def save_edges(data_dir: Path, edges: list[LexiconEdge]) -> Path:
    """Rewrite the entire edge list (idempotent).

    .. note::
       This function *ignores* the on-disk state and overwrites with the
       given edges as a whole. In multi-process environments, single-edge
       partial updates (add/remove) must use :func:`add_edge` to be safe
       from loss — stage Z.9 (2026-05-03).
    """

    path = edges_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, _render_edges_text(edges))
    return path


def add_edge(data_dir: Path, edge: LexiconEdge) -> Path:
    """Add a single edge. If a row with the same (kind, source, target)
    exists, increase its weight (additive).

    Stage Z.9 (2026-05-03) — multi-process safe:
    ``transactional_rmw`` performs an atomic read-modify-write inside a
    FileLock. Concurrent ``add_edge`` calls from other processes lose
    nothing.
    """

    path = edges_path(data_dir)

    def _mutator(text: str) -> str:
        edges = _parse_edges_from_text(text)
        for i, e in enumerate(edges):
            if (e.kind, e.source, e.target) == (edge.kind, edge.source, edge.target):
                edges[i] = LexiconEdge(
                    kind=e.kind,
                    source=e.source,
                    target=e.target,
                    weight=min(10.0, e.weight + edge.weight),
                    session_id=edge.session_id or e.session_id,
                    ts_iso=edge.ts_iso or e.ts_iso,
                )
                break
        else:
            edges.append(edge)
        return _render_edges_text(edges)

    transactional_rmw(path, _mutator)
    return path


# ---------------------------------------------------------------------------
# Index (_user_lexicon/_index.md)
# ---------------------------------------------------------------------------


_TIER_TO_DIR: dict[str, str] = {
    "semantic": SEMANTIC_DIR,
    "episodic": EPISODIC_DIR,
    "legacy": LEGACY_NODES_DIR,
}


def render_index(entries: list[tuple[str, PhraseNode]]) -> str:
    """Body of ``_index.md`` — a matrix for fast lookup by surface form.

    Each element of ``entries`` is a ``(tier, node)`` pair, where tier is
    in ``{'semantic', 'episodic', 'legacy'}``. The link prefix points to
    the tier's actual on-disk location (``semantic/`` / ``episodic/`` /
    ``nodes/``) — restoring consistency after Q.1's two-tier introduction,
    when links used to point at the legacy ``nodes/`` (stage R, fix 3).
    """

    fm: dict[str, object] = {
        "type": "lexicon_index",
        "_authored_by": "stats",
        "node_count": len(entries),
    }
    sorted_entries = sorted(
        entries, key=lambda e: (-e[1].strength, -e[1].seen_count, e[1].surface)
    )
    body_lines = [
        "# Lexicon Index — user surface form -> node",
        "",
        "> Entry point for the AI. Sorted by strength descending. Surface"
        " is shown in its *original spelling*.",
        "",
        "| surface | seen | strength | last_seen | node |",
        "|---|---|---|---|---|",
    ]
    for tier, n in sorted_entries:
        tier_dir = _TIER_TO_DIR.get(tier)
        if tier_dir is None:
            raise ValueError(f"unknown lexicon tier: {tier!r}")
        rel_path = f"{tier_dir}/{n.key}.md"
        body_lines.append(
            f"| {n.surface} | {n.seen_count} | {n.strength:.2f} | "
            f"{_ns_iso(n.last_seen_ns)} | [{n.key}]({rel_path}) |"
        )
    body_lines.append("")
    return _fm_render(fm, "\n".join(body_lines))


def refresh_index(data_dir: Path) -> Path:
    """Regenerate ``_user_lexicon/_index.md`` by scanning across all three
    tiers (semantic / episodic / legacy).

    Q.1: when the same phrase_key exists in two tiers, take the node from
    the *higher-priority* tier (semantic) and ignore the others (a signal
    that consistency has been broken — the next promote/demote will tidy
    it up).
    """

    entries_by_key: dict[str, tuple[str, PhraseNode]] = {}
    for tier, p in iter_all_nodes(data_dir):
        try:
            fm, _ = _fm_parse(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if fm.get("type") != "phrase":
            continue
        surf = str(fm.get("surface", ""))
        if not surf:
            continue
        node = PhraseNode(
            surface=surf,
            normalized=str(fm.get("normalized", _normalize(surf))),
            first_seen_ns=int(fm.get("first_seen_ns", 0)),
            last_seen_ns=int(fm.get("last_seen_ns", 0)),
            seen_count=int(fm.get("seen_count", 0)),
            session_ids=[str(s) for s in fm.get("session_ids", [])],
            intent_tags=[str(t) for t in fm.get("intent_tags", [])],
            strength=float(fm.get("strength", 0.30)),
        )
        # iter_all_nodes yields in semantic -> episodic -> legacy order, so
        # take only the *first occurrence* of each key.
        entries_by_key.setdefault(node.key, (tier, node))

    target = lexicon_dir(data_dir) / "_index.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, render_index(list(entries_by_key.values())))
    return target


__all__ = [
    "EDGE_KINDS",
    "EPISODIC_DIR",
    "LEGACY_NODES_DIR",
    "SEMANTIC_DIR",
    "EdgeKind",
    "LexiconEdge",
    "PhraseNode",
    "add_edge",
    "edges_path",
    "episodic_dir",
    "find_node_path",
    "iter_all_nodes",
    "lexicon_dir",
    "load_edges",
    "load_node",
    "node_path",
    "node_path_in",
    "nodes_dir",
    "phrase_key",
    "promote_to_semantic",
    "refresh_index",
    "render_index",
    "save_edges",
    "save_node",
    "semantic_dir",
    "upsert_phrase",
]

from __future__ import annotations

"""Placement helpers — derive candidates / recommendations + Markdown output for the write/read commands.

Principle alignment:
- D1 no search — show navigate's *space* candidates as-is (AI/user selects).
- D2 no semantic decision — drawer/section name recommendations are simple
  cue/kind slugs. No semantic matching. The user/AI makes the final pick.
- V3 byte — read returns the exact section body only when there is 1
  candidate; with N candidates, body is 0 bytes.
"""

import re
from dataclasses import dataclass
from typing import Any


# Allowed write intent kinds. Not validated — any free slug is also accepted.
KNOWN_KINDS: frozenset[str] = frozenset(
    {
        "memo",
        "decision",
        "rule",
        "session",
        "knowledge",
        "source",
        "todo",
        "revision",
    }
)


_SLUG_TRIM = re.compile(r"[^a-z0-9\-]+")
_SLUG_DASH = re.compile(r"-+")


def slugify(text: str, *, max_words: int = 4) -> str:
    """Simple lower-kebab English slug (only ASCII alphanumerics — Korean chars are dropped).

    Args:
        text: Free-form input (cue / first body line / etc.).
        max_words: Max number of ASCII alphanumeric tokens to include.

    Returns:
        Slug. Empty if nothing usable.
    """

    if not text:
        return ""
    lowered = text.lower()
    # Extract only ASCII alphanumeric tokens (Korean chars are not put into slugs — D7 aligned).
    tokens = re.findall(r"[a-z0-9]+", lowered)
    if not tokens:
        return ""
    picked = tokens[:max_words]
    joined = "-".join(picked)
    joined = _SLUG_TRIM.sub("-", joined)
    joined = _SLUG_DASH.sub("-", joined).strip("-")
    return joined


@dataclass(frozen=True)
class Recommendation:
    """A new placement recommended by the write command.

    drawer and section_id can be overridden by the AI/user (--new-drawer / --new-section).
    """

    table: str
    drawer: str
    section: str

    def as_ref(self) -> str:
        return f"{self.table}/{self.drawer}#{self.section}"


def recommend_new_placement(
    *,
    cues: list[str],
    entities: list[str],
    kind: str | None,
    body_preview: str | None = None,
) -> Recommendation:
    """Recommend a new drawer + section slug from cue / kind / entity.

    No semantic matching — pure slugification. The AI makes the final decision.

    table policy: if kind is one of the known kinds, pluralize it (decisions/rules/...).
    Otherwise fall back to ``memos``.
    """

    # table = natural plural of kind (D7 semantic folder).
    table = "memos"
    if kind:
        kind_norm = kind.strip().lower()
        if kind_norm in KNOWN_KINDS:
            # decision -> decisions, rule -> rules, todo -> todos, etc.
            table = kind_norm if kind_norm.endswith("s") else kind_norm + "s"
        elif kind_norm:
            table = slugify(kind_norm) or "memos"

    # drawer = first cue or first entity slug (else "general").
    seed_drawer = ""
    for src in cues + entities:
        seed_drawer = slugify(src, max_words=3)
        if seed_drawer:
            break
    if not seed_drawer:
        # Try the first line of body preview as a fallback.
        if body_preview:
            first_line = body_preview.splitlines()[0] if body_preview else ""
            seed_drawer = slugify(first_line, max_words=3)
    if not seed_drawer:
        seed_drawer = "general"

    # section = first cue (if any) + 1 entity token (if any) — falls back to "note" if too short
    section_seed = ""
    if cues:
        section_seed = slugify(cues[0], max_words=4)
    if not section_seed and entities:
        section_seed = slugify(entities[0], max_words=4)
    if not section_seed and body_preview:
        first_line = body_preview.splitlines()[0] if body_preview else ""
        section_seed = slugify(first_line, max_words=4)
    if not section_seed:
        section_seed = "note"

    return Recommendation(table=table, drawer=seed_drawer, section=section_seed)


def parse_ref(ref: str) -> tuple[str, str, str]:
    """Parse ``<table>/<drawer>#<section>``.

    Raises:
        ValueError: Malformed input.
    """

    if "#" not in ref:
        raise ValueError(
            f"--ref format error: missing '#'. example: 'decisions/retrieval-policy#strict-v2'"
        )
    head, section = ref.rsplit("#", 1)
    if "/" not in head:
        raise ValueError(
            f"--ref format error: missing '/' (need to split table/drawer). example: 'decisions/retrieval-policy#strict-v2'"
        )
    table, drawer = head.split("/", 1)
    if not table or not drawer or not section:
        raise ValueError(f"--ref format error: empty component ({ref!r})")
    return table, drawer, section


def section_routes_from_navigate(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Pick only section-level routes from a navigate result (drops drawer/table-only fallbacks)."""

    return [
        r
        for r in (result.get("routes") or [])
        if r.get("table") and r.get("drawer") and r.get("section")
    ]


def render_candidates_md(
    candidates: list[dict[str, Any]],
    *,
    title: str = "## Existing candidates",
) -> list[str]:
    """Convert a list of candidates into Markdown line list (echoed right before output)."""

    lines: list[str] = [title]
    if not candidates:
        lines.append("(none)")
        return lines
    for i, r in enumerate(candidates, start=1):
        ref = f"{r.get('table')}/{r.get('drawer')}#{r.get('section')}"
        signals = r.get("signals") or []
        sig_str = ", ".join(str(s) for s in signals) if signals else ""
        reason = (r.get("reason") or "").strip().splitlines()[0] if r.get("reason") else ""
        suffix = ""
        if sig_str:
            suffix = f" — signals: {sig_str}"
        elif reason:
            suffix = f" — reason: {reason}"
        lines.append(f"{i}. {ref}{suffix}")
    return lines


def render_recommendation_md(rec: Recommendation) -> list[str]:
    return [
        "## Recommended new placement",
        f"drawer: {rec.table}/{rec.drawer}",
        f"section: {rec.section}",
        f"ref: {rec.as_ref()}",
    ]


def compose_query(
    *,
    cues: list[str],
    entities: list[str],
    kind: str | None,
) -> str:
    """Compose a cue string for the write command to pass to navigate. Empty input -> empty string."""

    parts: list[str] = []
    parts.extend(c for c in cues if c)
    parts.extend(e for e in entities if e)
    if kind:
        parts.append(kind)
    return " ".join(parts).strip()

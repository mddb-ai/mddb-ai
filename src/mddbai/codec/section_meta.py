from __future__ import annotations

"""Stage X (2026-05-06) — section-level metadata schema.

Per-section navigation metadata is embedded in the ``sections_meta:`` dict inside the
drawer's frontmatter. 4 slots: ``cue / importance / related / memory_zone``.

Storage format (drawer .md frontmatter)::

    ---
    _drawer_id: decisions/identity
    _kind: drawer
    sections_meta:
      v1-lock:
        cue: [v1, lock, 2026-05-05]
        importance: 0.9
        related: [decisions/dogma, decisions/migration/2026-05]
        memory_zone: hot
      stage-r:
        cue: [stage-R, bug-fix]
        importance: 0.5
        related: []
        memory_zone: warm
    ---
    ## v1-lock
    ...

Principle alignment:
- D1 no search — cue is *a trace written by the AI*. DB does not auto-extract.
- D2 no decisions — importance / memory_zone decisions = AI's responsibility. DB only provides *space*.
- token equivalence — string written into frontmatter is read as-is = byte identical.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field, asdict
from typing import Any

from mddbai.core.errors import MddbError

# cue compression threshold (U.5 — prevents frontmatter explosion)
# When any of the following holds, write cue in the cue_tokens compressed form:
# 1. Some cue entry contains whitespace (a rich cue with multiple words in one entry).
# 2. Sum of cue entry character lengths exceeds 30 (corresponds to 5+ words).
# Goal: keep frontmatter under 100 bytes per section. 50000 sections * 100B = 5MB.
_CUE_COMPRESS_THRESHOLD = 30

# Maximum number of cue_tokens to extract
_CUE_MAX_TOKENS = 2

# stopword set — removes meaningless particles / conjunctions / articles
_CUE_STOPWORDS: frozenset[str] = frozenset({
    # Korean particles / conjunctions / adverbs
    "이", "가", "은", "는", "을", "를", "의", "에", "에서", "에게", "와", "과",
    "으로", "로", "도", "만", "도", "부터", "까지", "보다", "처럼", "같이",
    "하고", "이고", "이며", "그리고", "그러나", "하지만", "그래서", "그런데",
    "및", "또는", "혹은", "즉", "곧", "결국", "따라서",
    # English articles / prepositions / conjunctions
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "it", "its", "this", "that", "these", "those",
})

_CUE_TOKEN_SPLIT = re.compile(r"[\s\t\n]+", re.UNICODE)


def extract_cue_tokens(cue_list: list[str], max_tokens: int = _CUE_MAX_TOKENS) -> list[str]:
    """Extract 1-2 core noun tokens from a cue list.

    Heuristics:
    - whitespace split -> remove stopwords -> drop length<=1 -> first max_tokens.
    - Korean/English mix OK. Original case preserved.
    - duplicate removal (case-insensitive).

    D2 alignment: no semantic decision — purely position-based (first N). Assumes the AI
    has already placed the core word at the front of the cue list (a trace pattern written by the AI).
    """

    seen_lower: set[str] = set()
    tokens: list[str] = []
    for phrase in cue_list:
        for raw_tok in _CUE_TOKEN_SPLIT.split(phrase):
            tok = raw_tok.strip()
            if not tok or len(tok) <= 1:
                continue
            if tok.lower() in _CUE_STOPWORDS:
                continue
            if tok.lower() in seen_lower:
                continue
            seen_lower.add(tok.lower())
            tokens.append(tok)
            if len(tokens) >= max_tokens:
                return tokens
    return tokens


# System reserved keys — cannot be embedded inside the section-meta dict.
_RESERVED_KEYS = frozenset({
    "_kind",
    "_drawer_id",
    "_sections",
    "_content_hash",
    "_lsn",
    "_target",
    "_deleted_sections",
})

VALID_MEMORY_ZONES = ("hot", "warm", "cold", "archive")
VALID_STATES = ("active", "superseded", "deprecated")
VALID_SOURCES = ("ai", "user", "cite", "tool", "import")

# Typed relation kinds (2026-05-09).
# AI assigns the kind at write time. DB only validates the label and stores it.
# D2 alignment: meaning of "refines" vs "implies" is the AI's call.
VALID_RELATION_KINDS = (
    "refines",
    "supersedes",
    "contradicts",
    "implies",
    "depends-on",
    "derived-from",
)

# Language code validation — 2-8 lowercase letters, one hyphen allowed (en-us etc.).
# No strict whitelist — D2 alignment, AI / user free decision.
_LANG_PATTERN = re.compile(r"^[a-z]{2,3}(-[a-z]{2,4})?$")

# Hangul syllable block U+AC00-U+D7A3 + Jamo block — used for auto-detect.
_HANGUL_PATTERN = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")


def detect_lang(*texts: str) -> str:
    """Take body / cue / entity etc. and return a language inference hint.

    If even one Hangul syllable appears, ``"ko"``. Otherwise ``"en"``. Embed only one *hint*
    so cold AI can perform cross-language inference at recall time — semantic decision is
    the AI's responsibility (D2 alignment).

    Args:
        *texts: target strings to inspect (body / cue / section_id etc.).

    Returns:
        ``"ko"`` or ``"en"`` (currently only these two labels auto-detected).
    """

    for t in texts:
        if t and _HANGUL_PATTERN.search(t):
            return "ko"
    return "en"


class SectionMetadataError(MddbError):
    """Section metadata parse / validation failure."""


@dataclass(slots=True)
class Relation:
    """Typed relation edge to another section/drawer (2026-05-09).

    Coexists with the legacy untyped ``related`` list. Use ``related`` for
    plain adjacency (no semantic kind), use ``relations`` when the AI wants
    to mark the *kind* of relationship (refines / supersedes / contradicts
    / implies / depends-on / derived-from). DB validates ``kind`` against
    VALID_RELATION_KINDS only — meaning is the AI's call (D2).
    """

    target: str
    kind: str

    def to_dict(self) -> dict[str, str]:
        return {"target": self.target, "kind": self.kind}


@dataclass(slots=True)
class SectionMetadata:
    """Navigation metadata for one H2 section. All optional — when present, contributes to navigate score.

    Multi-representation cue (2026-05-07): place *multiple kinds of cue*
    (predicate cue + noun entity + temporal date + provenance source) on one section so
    diverse natural-language hints all converge on the same section.

    Revision protocol (2026-05-07): ``state`` / ``current_revision`` /
    ``supersedes`` separate active vs superseded. recall returns active only by default.

    Attributes:
        cue: recall hints (English/Korean free, traces written by the AI). May be empty.
        importance: 0.0 ~ 1.0 (importance assigned by the AI). None if unset.
        related: list of adjacent drawers or paths in ``<table>/<drawer>`` form.
        memory_zone: one of hot / warm / cold / archive. None if unset.
        entity: noun-form keywords (people/places/concepts/products). Written by the AI.
        date: ISO 8601 date or ``YYYY-MM-DD``. Event time.
        source: ai / user / cite / tool / import — provenance category.
        confidence: 0.0 ~ 1.0 — confidence assigned by the AI (separate from importance).
        state: active / superseded / deprecated.
        current_revision: latest revision identifier (e.g. ``r3``). None = single revision.
        supersedes: list of old revision ids superseded by this section.
    """

    cue: list[str] = field(default_factory=list)
    importance: float | None = None
    related: list[str] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    """Typed relation edges (2026-05-09).

    Coexists with ``related``. Used when the AI wants to mark the *kind*
    of relationship instead of bare adjacency. AI decides the kind, DB
    only validates the label (D1/D2 alignment).
    """
    memory_zone: str | None = None
    entity: list[str] = field(default_factory=list)
    date: str | None = None
    source: str | None = None
    confidence: float | None = None
    state: str | None = None
    current_revision: str | None = None
    supersedes: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    """Korean <-> English <-> abbreviation cue mapping (user-written 2026-05-08).

    Example: at section slug ``PreparationPhase``:
        aliases = ['1막', 'first-act', 'preparation', 'phase1', 'act1']

    When the AI receives a Korean cue, this slot holds info that lets it infer the English
    slug location. Written at write time (--alias option). Dumped by list-sections / cues.
    """
    chosen_because: str | None = None
    """One line on why this slot was chosen (user-written 2026-05-08).

    Reasoning trace written at write time. The next cold AI receives it together with the
    body on take / read and continues from the previous AI's reasoning path. Strong cold-start entry point.

    Example: chosen_because = "decision to delay Q4 launch — supply chain risk + marketing prep"
    """
    lang: str | None = None
    """Language inference hint (user decision 2026-05-08).

    On write, the language of body + cue is embedded as a 1-letter hint (``ko`` / ``en`` etc.).
    On recall, cold AI sees this hint in the ``cues`` dump and performs cross-language inference
    — the DB does not match, the AI infers (D1/D2 alignment).

    Storage uses only one of the user-provided languages. The forced both-Korean-and-English embedding is gone — the trace is a lightweight 1-letter hint.

    Example: ``lang = "ko"`` (Korean body), ``lang = "en"`` (English body).
    """

    def to_dict(self) -> dict[str, Any]:
        """Dict for frontmatter serialization. None / empty lists are *omitted* (cat view tidiness).

        U.5 cue compression policy:
        - if sum of cue entry character lengths exceeds ``_CUE_COMPRESS_THRESHOLD`` (50)
          -> embed only ``cue_tokens`` (1-2 words) in frontmatter and omit original ``cue``.
        - otherwise -> embed original ``cue`` as before.
        - the rich original cue is a slot for the AI to embed directly inside the H2 section body (cat visibility preserved).
        """

        out: dict[str, Any] = {}
        if self.cue:
            # Compression condition: any whitespace-containing entry, or total length > 30
            has_multi_word = any(" " in c or "\t" in c for c in self.cue)
            cue_total_len = sum(len(c) for c in self.cue)
            if has_multi_word or cue_total_len > _CUE_COMPRESS_THRESHOLD:
                # compressed path: embed only cue_tokens
                tokens = extract_cue_tokens(self.cue)
                if tokens:
                    out["cue_tokens"] = tokens
            else:
                # short path: original cue as-is
                out["cue"] = list(self.cue)
        if self.importance is not None:
            out["importance"] = float(self.importance)
        if self.related:
            out["related"] = list(self.related)
        if self.relations:
            out["relations"] = [r.to_dict() for r in self.relations]
        if self.memory_zone is not None:
            out["memory_zone"] = self.memory_zone
        if self.entity:
            out["entity"] = list(self.entity)
        if self.date is not None:
            out["date"] = self.date
        if self.source is not None:
            out["source"] = self.source
        if self.confidence is not None:
            out["confidence"] = float(self.confidence)
        if self.state is not None:
            out["state"] = self.state
        if self.current_revision is not None:
            out["current_revision"] = self.current_revision
        if self.supersedes:
            out["supersedes"] = list(self.supersedes)
        if self.aliases:
            out["aliases"] = list(self.aliases)
        if self.chosen_because is not None:
            out["chosen_because"] = self.chosen_because
        if self.lang is not None:
            out["lang"] = self.lang
        return out

    def merge(self, other: SectionMetadata) -> SectionMetadata:
        """Fill *only the empty slots* with ``other`` values (not override)."""

        return SectionMetadata(
            cue=list(self.cue) if self.cue else list(other.cue),
            importance=self.importance if self.importance is not None else other.importance,
            related=list(self.related) if self.related else list(other.related),
            relations=list(self.relations) if self.relations else list(other.relations),
            memory_zone=self.memory_zone if self.memory_zone is not None else other.memory_zone,
            entity=list(self.entity) if self.entity else list(other.entity),
            date=self.date if self.date is not None else other.date,
            source=self.source if self.source is not None else other.source,
            confidence=self.confidence if self.confidence is not None else other.confidence,
            state=self.state if self.state is not None else other.state,
            current_revision=(
                self.current_revision
                if self.current_revision is not None
                else other.current_revision
            ),
            supersedes=list(self.supersedes) if self.supersedes else list(other.supersedes),
            aliases=list(self.aliases) if self.aliases else list(other.aliases),
            chosen_because=(
                self.chosen_because
                if self.chosen_because is not None
                else other.chosen_because
            ),
            lang=self.lang if self.lang is not None else other.lang,
        )


def parse_section_metadata(raw: Any) -> SectionMetadata:
    """frontmatter single-section metadata dict -> SectionMetadata.

    None / empty dict returns an empty SectionMetadata.

    Raises:
        SectionMetadataError: type mismatch / invalid value.
    """

    if raw is None:
        return SectionMetadata()
    if not isinstance(raw, dict):
        raise SectionMetadataError(
            f"section metadata must be a mapping, got {type(raw).__name__}"
        )

    # U.5 backward-compat: read both old format (raw cue) and new format (cue_tokens compressed).
    # If cue_tokens is present, read it first; otherwise read original cue.
    cue_tokens_raw = raw.get("cue_tokens")
    if cue_tokens_raw is not None:
        cue = _coerce_str_list(cue_tokens_raw, "cue_tokens")
    else:
        cue_raw = raw.get("cue", [])
        cue = _coerce_str_list(cue_raw, "cue")

    importance_raw = raw.get("importance")
    importance: float | None
    if importance_raw is None:
        importance = None
    else:
        try:
            importance = float(importance_raw)
        except (TypeError, ValueError) as exc:
            raise SectionMetadataError(
                f"importance must be float, got {importance_raw!r}"
            ) from exc
        if not 0.0 <= importance <= 1.0:
            raise SectionMetadataError(
                f"importance must be in [0.0, 1.0], got {importance}"
            )

    related_raw = raw.get("related", [])
    related = _coerce_str_list(related_raw, "related")

    relations_raw = raw.get("relations", [])
    relations = _coerce_relation_list(relations_raw)

    zone_raw = raw.get("memory_zone")
    if zone_raw is None or zone_raw == "":
        memory_zone: str | None = None
    elif not isinstance(zone_raw, str):
        raise SectionMetadataError(
            f"memory_zone must be string, got {type(zone_raw).__name__}"
        )
    elif zone_raw not in VALID_MEMORY_ZONES:
        raise SectionMetadataError(
            f"memory_zone must be one of {VALID_MEMORY_ZONES}, got {zone_raw!r}"
        )
    else:
        memory_zone = zone_raw

    entity_raw = raw.get("entity", [])
    entity = _coerce_str_list(entity_raw, "entity")

    date_raw = raw.get("date")
    if date_raw is None or date_raw == "":
        date: str | None = None
    elif isinstance(date_raw, str):
        date = date_raw.strip() or None
    else:
        # Types other than ISO-compatible string (e.g. datetime) are normalized via str()
        date = str(date_raw)

    source_raw = raw.get("source")
    if source_raw is None or source_raw == "":
        source: str | None = None
    elif not isinstance(source_raw, str):
        raise SectionMetadataError(
            f"source must be string, got {type(source_raw).__name__}"
        )
    elif source_raw not in VALID_SOURCES:
        raise SectionMetadataError(
            f"source must be one of {VALID_SOURCES}, got {source_raw!r}"
        )
    else:
        source = source_raw

    confidence_raw = raw.get("confidence")
    confidence: float | None
    if confidence_raw is None:
        confidence = None
    else:
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError) as exc:
            raise SectionMetadataError(
                f"confidence must be float, got {confidence_raw!r}"
            ) from exc
        if not 0.0 <= confidence <= 1.0:
            raise SectionMetadataError(
                f"confidence must be in [0.0, 1.0], got {confidence}"
            )

    state_raw = raw.get("state")
    if state_raw is None or state_raw == "":
        state: str | None = None
    elif not isinstance(state_raw, str):
        raise SectionMetadataError(
            f"state must be string, got {type(state_raw).__name__}"
        )
    elif state_raw not in VALID_STATES:
        raise SectionMetadataError(
            f"state must be one of {VALID_STATES}, got {state_raw!r}"
        )
    else:
        state = state_raw

    cur_rev_raw = raw.get("current_revision")
    if cur_rev_raw is None or cur_rev_raw == "":
        current_revision: str | None = None
    elif isinstance(cur_rev_raw, str):
        current_revision = cur_rev_raw.strip() or None
    else:
        current_revision = str(cur_rev_raw)

    supersedes_raw = raw.get("supersedes", [])
    supersedes = _coerce_str_list(supersedes_raw, "supersedes")

    aliases_raw = raw.get("aliases", [])
    aliases = _coerce_str_list(aliases_raw, "aliases")

    because_raw = raw.get("chosen_because")
    chosen_because: str | None
    if because_raw is None or because_raw == "":
        chosen_because = None
    elif isinstance(because_raw, str):
        chosen_because = because_raw.strip() or None
    else:
        chosen_because = str(because_raw)

    lang_raw = raw.get("lang")
    lang: str | None
    if lang_raw is None or lang_raw == "":
        lang = None
    elif not isinstance(lang_raw, str):
        raise SectionMetadataError(
            f"lang must be string, got {type(lang_raw).__name__}"
        )
    else:
        normalized = lang_raw.strip().lower()
        if not normalized:
            lang = None
        elif not _LANG_PATTERN.match(normalized):
            raise SectionMetadataError(
                f"lang must match {_LANG_PATTERN.pattern!r} (e.g. 'ko', 'en', 'en-us'), got {lang_raw!r}"
            )
        else:
            lang = normalized

    return SectionMetadata(
        cue=cue,
        importance=importance,
        related=related,
        relations=relations,
        memory_zone=memory_zone,
        entity=entity,
        date=date,
        source=source,
        confidence=confidence,
        state=state,
        current_revision=current_revision,
        supersedes=supersedes,
        aliases=aliases,
        chosen_because=chosen_because,
        lang=lang,
    )


def _coerce_relation_list(value: Any) -> list[Relation]:
    """Parse the ``relations`` field into a list of Relation objects.

    Each entry must be a mapping with ``target`` (non-empty string) and
    ``kind`` (one of VALID_RELATION_KINDS). Invalid entries raise
    SectionMetadataError. None / empty list returns [].
    """

    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise SectionMetadataError(
            f"relations must be a list, got {type(value).__name__}"
        )
    out: list[Relation] = []
    for item in value:
        if not isinstance(item, dict):
            raise SectionMetadataError(
                f"relations entries must be mappings, got {type(item).__name__}"
            )
        target = item.get("target")
        kind = item.get("kind")
        if not isinstance(target, str) or not target.strip():
            raise SectionMetadataError(
                f"relations entry missing non-empty 'target' string"
            )
        if not isinstance(kind, str) or not kind.strip():
            raise SectionMetadataError(
                f"relations entry missing non-empty 'kind' string"
            )
        kind_norm = kind.strip()
        if kind_norm not in VALID_RELATION_KINDS:
            raise SectionMetadataError(
                f"relations.kind must be one of {VALID_RELATION_KINDS}, "
                f"got {kind_norm!r}"
            )
        out.append(Relation(target=target.strip(), kind=kind_norm))
    return out


def _coerce_str_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # Allow single string too (convenience)
        v = value.strip()
        return [v] if v else []
    if not isinstance(value, (list, tuple)):
        raise SectionMetadataError(
            f"{name} must be a list, got {type(value).__name__}"
        )
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            v = item.strip()
            if v:
                out.append(v)
        elif isinstance(item, (int, float, bool)):
            out.append(str(item))
        else:
            raise SectionMetadataError(
                f"{name} entries must be strings, got {type(item).__name__}"
            )
    return out


def parse_sections_meta(meta: dict[str, Any]) -> dict[str, SectionMetadata]:
    """Parse ``sections_meta`` from a drawer frontmatter dict.

    If ``sections_meta`` is missing or not a dict, returns empty dict.

    Raises:
        SectionMetadataError: child entry has invalid form.
    """

    raw = meta.get("sections_meta")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SectionMetadataError(
            f"sections_meta must be a mapping, got {type(raw).__name__}"
        )
    out: dict[str, SectionMetadata] = {}
    for sid, section_raw in raw.items():
        if not isinstance(sid, str) or not sid:
            continue
        out[sid] = parse_section_metadata(section_raw)
    return out


def serialize_sections_meta(
    sections_meta: dict[str, SectionMetadata],
) -> dict[str, dict[str, Any]] | None:
    """SectionMetadata dict -> frontmatter serialization dict.

    Empty entries omitted. If the result dict is empty, returns ``None`` (signal
    not to embed in frontmatter).
    """

    out: dict[str, dict[str, Any]] = {}
    for sid, sm in sections_meta.items():
        d = sm.to_dict()
        if d:
            out[sid] = d
    return out or None


def validate_section_id(sid: str) -> None:
    """Validate section id. Empty / newline / system reserved word forbidden."""

    if not isinstance(sid, str) or not sid:
        raise SectionMetadataError("section id must be non-empty string")
    if "\n" in sid or "\r" in sid:
        raise SectionMetadataError("section id must not contain newline")
    if sid in _RESERVED_KEYS:
        raise SectionMetadataError(
            f"section id {sid!r} conflicts with system reserved key"
        )


# ---- structural cue derivation -----------------------------------------
#
# 2026-05-07 — strict retrieval discipline.
# If the caller does not specify cue, build a conservative cue from *structure* alone
# (section id tokens + drawer rel path tokens), not search/embedding. No semantic analysis
# of body (D2 - DB does not decide meaning). Even if preview comes in, slice the first N characters
# as-is and put it in as one entry (trace preservation, not semantic extraction).

_STRUCTURAL_TOKEN_SPLIT = re.compile(r"[\s/_\-.,;:!?'\"\(\)\[\]\{\}<>|]+", re.UNICODE)
_STRUCTURAL_MIN_LEN = 2
_STRUCTURAL_MAX_TOKENS = 8
_STRUCTURAL_PREVIEW_CAP = 80


def derive_structural_cue(
    *,
    section_id: str,
    drawer_rel: str = "",
    heading: str = "",
    body_preview: str = "",
) -> list[str]:
    """Generate structure-based cue candidates. No semantic extraction (D2 alignment).

    - tokenization targets: section_id + drawer_rel + heading.
    - tokens with length < ``_STRUCTURAL_MIN_LEN`` are dropped as noise.
    - duplicate removal (insertion order kept).
    - if body_preview is given, only the first ``_STRUCTURAL_PREVIEW_CAP`` characters are added
      *as-is* as one entry (no analysis — trace preservation).

    Returns:
        ``list[str]`` — conservative cue candidates (max ``_STRUCTURAL_MAX_TOKENS`` + 1).
    """

    sources = [section_id or "", drawer_rel or "", heading or ""]
    seen: set[str] = set()
    out: list[str] = []
    for src in sources:
        if not src:
            continue
        for tok in _STRUCTURAL_TOKEN_SPLIT.split(src):
            t = tok.strip().lower()
            if len(t) < _STRUCTURAL_MIN_LEN:
                continue
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
            if len(out) >= _STRUCTURAL_MAX_TOKENS:
                break
        if len(out) >= _STRUCTURAL_MAX_TOKENS:
            break

    if body_preview:
        snippet = body_preview.strip().replace("\n", " ")
        if len(snippet) > _STRUCTURAL_PREVIEW_CAP:
            snippet = snippet[:_STRUCTURAL_PREVIEW_CAP]
        if snippet and snippet.lower() not in seen:
            out.append(snippet)

    return out


__all__ = [
    "VALID_MEMORY_ZONES",
    "VALID_RELATION_KINDS",
    "Relation",
    "SectionMetadata",
    "SectionMetadataError",
    "derive_structural_cue",
    "detect_lang",
    "extract_cue_tokens",
    "parse_section_metadata",
    "parse_sections_meta",
    "serialize_sections_meta",
    "validate_section_id",
]

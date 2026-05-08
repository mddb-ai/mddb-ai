from __future__ import annotations

"""K.2 — User utterance extractor.

No LLM calls. Three-layer extraction using only morphology, regexes, and
a small dictionary:

- L1 key phrases: noun / verb phrases (n-gram 2-4, stopwords removed,
  whitespace tokens)
- L2 user signal words: matches against the ``_user_lexicon/_signals.md``
  dictionary -> intent / decision / reject
- L3 deictic: fixed token dictionary ("that", "this", "earlier",
  "yesterday", etc.)

On the sleep job's first run the default signal dictionary is written to
``_user_lexicon/_signals.md`` on disk. The user can edit it to add their
own vocabulary.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Default signal dictionary (English seed; users can edit on disk)
# ---------------------------------------------------------------------------

#: tag -> list of signal-word candidates
DEFAULT_SIGNALS: dict[str, tuple[str, ...]] = {
    "decision": ("decide", "confirmed", "let's go with", "go with this", "go ahead", "OK"),
    "reject": ("no", "not that", "remove", "delete", "cancel", "scrap that"),
    "request_design": ("how to build", "design it", "build it", "how to do", "approach"),
    "request_example": ("example", "for example", "give an example"),
    "brainstorm": ("brainstorm", "[brainstorm]", "discussion mode", "let's discuss"),
    "confirm": ("right", "correct", "yes", "yep", "yeah"),
}

#: Deictic tokens (exact substring match)
DEFAULT_DEICTIC: tuple[str, ...] = (
    "that one",
    "this one",
    "this",
    "that",
    "earlier",
    "yesterday",
    "last time",
    "previous",
    "that way",
    "that part",
    "that case",
)

#: English stopwords (extendable)
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "is", "are", "was", "were", "be", "to", "of", "and",
        "or", "in", "on", "for", "with", "by", "at", "from", "this", "that",
        "it", "its", "as", "but", "if", "so", "do", "does", "did", "have",
        "has", "had", "will", "would", "can", "could", "should", "may", "might",
    }
)


@dataclass(frozen=True, slots=True)
class SignalHit:
    """L2 signal-word match result."""

    tag: str
    surface: str  # Matched surface form (e.g. "decide")


@dataclass(frozen=True, slots=True)
class ExtractedUtterance:
    """Extraction result — L1 / L2 / L3 + meta."""

    phrases: list[str] = field(default_factory=list)
    signals: list[SignalHit] = field(default_factory=list)
    deictic: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.phrases or self.signals or self.deictic)


# ---------------------------------------------------------------------------
# Tokenizer (Korean-friendly — split on whitespace + punctuation)
# ---------------------------------------------------------------------------

_TOKEN_SPLIT_RE = re.compile(r"[\s,.\!?\?\-_/\\\(\)\[\]{}<>:;\"'`]+")
# Strip Korean particles attached to the end of a token (1-2 chars) — simple
_PARTICLE_RE = re.compile(
    r"(을|를|이|가|은|는|의|에|와|과|도|만|에서|으로|로|라|라고|고|하고|에게|한테|"
    r"부터|까지|이라|이라고|라고)$"
)


def tokenize(text: str) -> list[str]:
    """A simple Korean-friendly tokenizer.

    Split on whitespace and punctuation -> trim trailing particles -> drop
    empty tokens. The point is to work *without* a morphological analyzer
    (zero external dependencies).
    """

    raw = _TOKEN_SPLIT_RE.split(text)
    out: list[str] = []
    for tok in raw:
        if not tok:
            continue
        stripped = _PARTICLE_RE.sub("", tok)
        if not stripped:
            continue
        out.append(stripped)
    return out


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


def _extract_signals(text: str, signals: Mapping[str, Iterable[str]]) -> list[SignalHit]:
    out: list[SignalHit] = []
    seen: set[tuple[str, str]] = set()
    for tag, kws in signals.items():
        for kw in kws:
            if kw in text and (tag, kw) not in seen:
                out.append(SignalHit(tag=tag, surface=kw))
                seen.add((tag, kw))
    return out


def _extract_deictic(text: str, deictic: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for d in deictic:
        if d in text and d not in seen:
            out.append(d)
            seen.add(d)
    return out


def _extract_phrases(
    text: str,
    *,
    n_min: int = 2,
    n_max: int = 4,
    min_chars: int = 4,
    max_chars: int = 50,
) -> list[str]:
    tokens = tokenize(text)
    filtered = [t for t in tokens if t.lower() not in _STOPWORDS]
    seen: set[str] = set()
    out: list[str] = []
    for n in range(n_min, n_max + 1):
        for i in range(len(filtered) - n + 1):
            phrase = " ".join(filtered[i : i + n])
            if not (min_chars <= len(phrase) <= max_chars):
                continue
            if phrase in seen:
                continue
            seen.add(phrase)
            out.append(phrase)
    return out


def extract(
    text: str,
    *,
    signals: Mapping[str, Iterable[str]] | None = None,
    deictic: Iterable[str] | None = None,
) -> ExtractedUtterance:
    """Extract L1 / L2 / L3 from a raw utterance.

    Args:
        text: Masked utterance body
        signals: Custom signal-word dictionary (DEFAULT_SIGNALS if None)
        deictic: Custom deictic tokens (DEFAULT_DEICTIC if None)

    Returns:
        ``ExtractedUtterance(phrases=[...], signals=[...], deictic=[...])``
    """

    sig_dict: Mapping[str, Iterable[str]] = signals if signals is not None else DEFAULT_SIGNALS
    deic_list = deictic if deictic is not None else DEFAULT_DEICTIC

    return ExtractedUtterance(
        phrases=_extract_phrases(text),
        signals=_extract_signals(text, sig_dict),
        deictic=_extract_deictic(text, deic_list),
    )


# ---------------------------------------------------------------------------
# Disk IO for the signal-word dictionary
# ---------------------------------------------------------------------------


_SIGNALS_HEADER = """\
---
type: lexicon_signals
_authored_by: stats
description: |
  L2 signal-word dictionary of the user lexicon graph.
  Users can add or edit entries directly. Changing _authored_by to "ai"
  makes the sleep job's auto-refresh preserve this file.
---

# User signal-word dictionary (L2)

This dictionary is used to detect *intent signals* in user utterances.
Each tag has one or more surface forms. When a surface matches, the
utterance's intent is taken as that tag.

"""


def render_signals_md(signals: Mapping[str, Iterable[str]]) -> str:
    """Build the body of ``_user_lexicon/_signals.md``."""

    lines = [_SIGNALS_HEADER]
    for tag, kws in signals.items():
        lines.append(f"## {tag}\n")
        for kw in kws:
            lines.append(f"- `{kw}`")
        lines.append("")
    return "\n".join(lines) + "\n"


def parse_signals_md(text: str) -> dict[str, list[str]]:
    r"""Parse the surface-form list back from ``_user_lexicon/_signals.md``.

    Format: a ``## <tag>`` header followed by a ``- \`<keyword>\``` list.
    """

    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## ") and len(stripped) > 3:
            current = stripped[3:].strip()
            out.setdefault(current, [])
            continue
        if current and stripped.startswith("- "):
            inner = stripped[2:].strip()
            if inner.startswith("`") and inner.endswith("`") and len(inner) >= 2:
                out[current].append(inner[1:-1])
    return {k: v for k, v in out.items() if v}


def ensure_signals_dictionary(
    data_dir: Path,
    *,
    refresh: bool = False,
) -> Path:
    """Create ``data/_user_lexicon/_signals.md`` with defaults if it is missing.

    Returns:
        Path of the file.
    """

    target = data_dir / "_user_lexicon" / "_signals.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not refresh:
        return target
    target.write_text(render_signals_md(DEFAULT_SIGNALS), encoding="utf-8")
    return target


def load_signals(data_dir: Path) -> dict[str, list[str]]:
    """Prefer the on-disk dictionary; fall back to DEFAULT if absent."""

    target = data_dir / "_user_lexicon" / "_signals.md"
    if target.exists():
        try:
            parsed = parse_signals_md(target.read_text(encoding="utf-8"))
        except OSError:
            parsed = {}
        if parsed:
            return parsed
    return {k: list(v) for k, v in DEFAULT_SIGNALS.items()}


__all__ = [
    "DEFAULT_DEICTIC",
    "DEFAULT_SIGNALS",
    "ExtractedUtterance",
    "SignalHit",
    "ensure_signals_dictionary",
    "extract",
    "load_signals",
    "parse_signals_md",
    "render_signals_md",
    "tokenize",
]

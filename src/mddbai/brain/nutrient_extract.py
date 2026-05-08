"""Nutrient extractor — pulls only *meaningful lines* from user utterances / AI responses.

Principle alignment:
- D1 (no search) — regex + keyword heuristics only. No embeddings / ML.
- D2 (semantic decisions = AI) — splitting and embedding lines is a spatial
  operation. Matching and meaning are the AI's responsibility.

Design (replaced 2026-05-08):
- Single-word / n-gram slots are *deprecated*. The user complained ("why are
  you slicing it up so much").
- Only nutrient *lines* (sentences) — lines containing decision signal words
  or insight expressions.
- AI response: ## H2 / ### H3 header bodies + signal-word lines.
- Code blocks / option tables excluded.

The extraction result is a list of ``NutrientLine``s. The hook receives them
and accumulates the writes into a single section of an mddbai drawer (no
per-word writing — one turn = one section).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Signal words (used to judge nutrient value)
# ---------------------------------------------------------------------------

DECISION_SIGNALS_KO: tuple[str, ...] = (
    "결정", "확정", "정하자", "갈음", "통과",
    "이걸로", "간다", "정합",
)

DECISION_SIGNALS_EN: tuple[str, ...] = (
    "decide", "decision", "confirm", "settle",
    "agreed", "lock-in",
)

INSIGHT_SIGNALS_KO: tuple[str, ...] = (
    "본질", "정체성", "원칙", "도그마",
    "차단점", "병목", "회피", "도돌이",
    "메인", "핵심", "본진",
)

ALL_SIGNALS = DECISION_SIGNALS_KO + DECISION_SIGNALS_EN + INSIGHT_SIGNALS_KO

# ---------------------------------------------------------------------------
# Regex / thresholds
# ---------------------------------------------------------------------------

H2_H3_PATTERN = re.compile(r"^#{2,3}\s+(.+)$", re.MULTILINE)
CODE_BLOCK_PATTERN = re.compile(r"```[\s\S]*?```", re.MULTILINE)
SENTENCE_SPLIT = re.compile(r"[.!?\n]+")
LIST_BULLET = re.compile(r"^\s*[-*]\s+")

MIN_TEXT_LENGTH = 8           # Block one-word answers / noise (always false below this)
MIN_LINE_LEN = 8              # Minimum length of a nutrient line
MAX_LINE_LEN = 200            # Maximum length of a nutrient line (cap)
LONG_TEXT_THRESHOLD = 50      # Pass even without signal words if at least this long
MAX_USER_LINES = 5            # Cap of nutrient lines per user turn
MAX_AI_LINES = 7              # Cap of nutrient lines per AI turn


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NutrientLine:
    """A single extracted nutrient line.

    Attributes:
        line: Cleaned line (trimmed, bullet removed).
        kind: ``decision`` | ``insight`` | ``header`` | ``general``.
        weight: 0.0~1.0. 1.0 if it contains a decision signal, 0.8 for
            insight, 0.7 for headers, 0.5 for general.
    """

    line: str
    kind: str
    weight: float


# ---------------------------------------------------------------------------
# Cue keyword extraction — meaningful words to embed in the frontmatter cue slot
# ---------------------------------------------------------------------------

# Word extraction (cue slot, single word)
CUE_WORD_PATTERN = re.compile(
    r"[가-힣A-Za-z][가-힣A-Za-z0-9_\-]{1,29}"
)

# Korean / English stopwords — never written into the cue slot
STOPWORDS_KO: frozenset[str] = frozenset({
    "이건", "그건", "저건", "이게", "그게", "저게",
    "이것", "그것", "저것", "이거", "그거", "저거",
    "여기", "거기", "저기", "이번", "그때", "이제",
    "그냥", "정말", "진짜", "아직", "벌써", "이미",
    "근데", "하지만", "그러나", "그래서", "그리고",
    "있다", "없다", "한다", "된다", "같다", "라고",
    "이다", "에서", "에게", "으로", "부터", "까지",
    "보다", "처럼", "만큼", "만약", "혹시",
})

STOPWORDS_EN: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "to",
    "in", "on", "at", "by", "of", "for", "with", "from",
    "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did",
    "this", "that", "these", "those", "it", "its",
    "we", "you", "he", "she", "they", "i", "me", "us",
    "can", "will", "would", "should", "could", "may",
    "not", "no", "yes", "ok", "so",
})

CUE_MIN_LEN = 2          # Minimum cue word length
CUE_MAX_LEN = 30
MAX_CUE_KEYWORDS = 12    # Cap on the frontmatter cue slot per turn


def extract_cue_keywords(lines: list[str]) -> list[str]:
    """Extract *meaningful keywords* from a batch of nutrient lines for the frontmatter cue slot.

    Extraction unit = a single word (Korean / English). Stopwords are excluded.
    Words that are too short or numeric-only are excluded.

    Args:
        lines: List of nutrient line bodies.

    Returns:
        List of cue keywords (deduplicated, max 12), sorted by frequency desc.
    """

    counts: dict[str, int] = {}
    for line in lines:
        for m in CUE_WORD_PATTERN.finditer(line):
            word = m.group(0)
            norm = word.lower().strip()

            # Length gate
            if not (CUE_MIN_LEN <= len(norm) <= CUE_MAX_LEN):
                continue
            # Numeric only
            if norm.replace("-", "").replace("_", "").isdigit():
                continue
            # Stopwords
            if norm in STOPWORDS_KO or norm in STOPWORDS_EN:
                continue

            counts[norm] = counts.get(norm, 0) + 1

    # Frequency desc + alphabetical
    sorted_keys = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _c in sorted_keys[:MAX_CUE_KEYWORDS]]


# ---------------------------------------------------------------------------
# Judgement
# ---------------------------------------------------------------------------

def is_nutrient_text(text: str) -> bool:
    """Does the utterance / response carry nutrient value?"""

    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < MIN_TEXT_LENGTH:
        return False

    lower = stripped.lower()

    for sig in ALL_SIGNALS:
        if sig.lower() in lower:
            return True

    return len(stripped) >= LONG_TEXT_THRESHOLD


def _classify_line(line: str) -> tuple[str, float] | None:
    """Return (kind, weight) for a line, or None if it carries no nutrient value."""

    lower = line.lower()
    for sig in DECISION_SIGNALS_KO + DECISION_SIGNALS_EN:
        if sig.lower() in lower:
            return ("decision", 1.0)
    for sig in INSIGHT_SIGNALS_KO:
        if sig.lower() in lower:
            return ("insight", 0.8)
    return None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _strip_code_blocks(text: str) -> str:
    """Strip code blocks (```...```) — they are not extraction targets."""

    return CODE_BLOCK_PATTERN.sub("", text)


def _normalize_line(line: str) -> str:
    """Trim ends + remove bullet + cap length."""

    line = LIST_BULLET.sub("", line.strip())
    if len(line) > MAX_LINE_LEN:
        line = line[:MAX_LINE_LEN].rstrip() + "..."
    return line


def extract_user_nutrient_lines(text: str) -> list[NutrientLine]:
    """Extract nutrient *lines* from a user utterance.

    Sentence-level. Only lines containing decision signal words / insight
    expressions are written. Don't write too many (max 5).
    """

    if not is_nutrient_text(text):
        return []

    cleaned = _strip_code_blocks(text)
    out: list[NutrientLine] = []
    seen: set[str] = set()

    for sentence in SENTENCE_SPLIT.split(cleaned):
        norm_line = _normalize_line(sentence)
        if not (MIN_LINE_LEN <= len(norm_line) <= MAX_LINE_LEN):
            continue
        if norm_line.lower() in seen:
            continue

        cls = _classify_line(norm_line)
        if cls is None:
            continue
        kind, weight = cls
        seen.add(norm_line.lower())
        out.append(NutrientLine(line=norm_line, kind=kind, weight=weight))

        if len(out) >= MAX_USER_LINES:
            break

    return out


def extract_ai_nutrient_lines(text: str) -> list[NutrientLine]:
    """Extract nutrient lines from an AI response.

    Priority order: H2 / H3 header bodies first. Then lines containing
    decision / insight signal words. Don't write too many (max 7).
    """

    if not text:
        return []

    cleaned = _strip_code_blocks(text)
    out: list[NutrientLine] = []
    seen: set[str] = set()

    # 1. H2 / H3 header bodies first
    for h in H2_H3_PATTERN.findall(cleaned):
        norm = _normalize_line(h)
        if not (MIN_LINE_LEN <= len(norm) <= MAX_LINE_LEN):
            continue
        if norm.lower() in seen:
            continue
        seen.add(norm.lower())
        out.append(NutrientLine(line=norm, kind="header", weight=0.7))
        if len(out) >= MAX_AI_LINES:
            return out

    # 2. Lines containing decision / insight signal words
    for raw_line in cleaned.split("\n"):
        norm = _normalize_line(raw_line)
        if not (MIN_LINE_LEN <= len(norm) <= MAX_LINE_LEN):
            continue
        if norm.lower() in seen:
            continue
        cls = _classify_line(norm)
        if cls is None:
            continue
        kind, weight = cls
        seen.add(norm.lower())
        out.append(NutrientLine(line=norm, kind=kind, weight=weight))
        if len(out) >= MAX_AI_LINES:
            break

    return out

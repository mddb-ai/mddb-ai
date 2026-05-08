from __future__ import annotations

"""Stage AA.4 (2026-05-03) — enforce english folder/file names.

Stops incoming AIs/users from creating Korean, uppercase, or whitespace
folder names at the entry point. The main approach is *encouragement*
(palace_init's english defaults + the naming-convention section in
agent_guide); this module is the *safety net* (final rejection plus an
english candidate suggestion).

Principles:

- Containers (folder names, file names, table, drawer, section_id, cluster id, etc.)
  must be lowercase english.
- Contents (body text, H2 titles, frontmatter values, tags) may be multilingual.
- On rejection, suggest an english candidate to the user/AI (suggest_english).

Aligned with the R4 readability principle — lowercase reduces visual
noise in cat output.
"""

import re
import unicodedata

ENGLISH_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
"""English identifier pattern. First char alphanumeric; rest ``a-z 0-9 _ -``; length 1~64."""

MAX_LENGTH = 64
"""Maximum identifier length."""

# Korean -> english candidate mapping (only the common ones; the rest fall
# back to transliteration).
_KOREAN_HINTS: dict[str, str] = {
    "기억": "memories",
    "메모": "notes",
    "노트": "notes",
    "할일": "tasks",
    "작업": "tasks",
    "회의": "meetings",
    "회의록": "meetings",
    "결정": "decisions",
    "문서": "docs",
    "코드": "code",
    "설정": "config",
    "사람": "people",
    "친구": "friends",
    "프로젝트": "projects",
    "참고": "refs",
    "참조": "refs",
    "학습": "learning",
    "내문서": "my_docs",
    "내메모": "my_notes",
    "일반": "general",
    "루트메타": "meta",
    "메타": "meta",
    "임시": "scratch",
    "실험": "experiments",
    "테스트": "tests",
    "백업": "backup",
    "보관": "archive",
}


def is_english_identifier(name: str) -> bool:
    """Return whether ``name`` matches the english identifier rule.

    Rule: ``^[a-z0-9][a-z0-9_-]{0,63}$``. First char alphanumeric, rest
    ``a-z 0-9 _ -``, length 1~64. Korean, uppercase, whitespace, special
    characters, dots, and slashes are all rejected.

    Args:
        name: identifier to inspect.

    Returns:
        ``True`` if the name passes, ``False`` if it is rejected.
    """

    if not name:
        return False
    return ENGLISH_IDENTIFIER_RE.match(name) is not None


def suggest_english(name: str) -> str:
    """Suggest the closest english candidate for ``name``.

    Korean dictionary lookup -> ASCII conversion -> lowercase + space/hyphen
    normalisation -> truncation.

    Args:
        name: original name potentially mixing Korean, uppercase, or whitespace.

    Returns:
        An english candidate (no guarantees — if the result looks odd ask
        the user to fix it directly). Empty input returns ``"item"``.
    """

    if not name.strip():
        return "item"

    s = name.strip()

    # 1) Whole-string Korean dictionary lookup
    if s in _KOREAN_HINTS:
        return _KOREAN_HINTS[s]

    # 2) Partial Korean match — longest match first
    for korean, english in sorted(_KOREAN_HINTS.items(), key=lambda kv: -len(kv[0])):
        if korean in s:
            s = s.replace(korean, english)

    # 3) NFKD decomposition then keep only ASCII (Korean -> empty, latin retained)
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_only = "".join(c for c in nfkd if ord(c) < 128)

    # 4) Lowercase + spaces / dots / slashes / others -> underscore
    lowered = ascii_only.lower()
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", lowered)

    # 5) Trim leading/trailing _ and -, collapse repeated _
    cleaned = re.sub(r"_+", "_", cleaned).strip("_-")

    # 6) Empty result / first char not alphanumeric -> fallback
    if not cleaned:
        return "item"
    if not cleaned[0].isalnum():
        cleaned = "x" + cleaned

    # 7) Length limit
    if len(cleaned) > MAX_LENGTH:
        cleaned = cleaned[:MAX_LENGTH].rstrip("_-")

    return cleaned or "item"


def validate_english_identifier(name: str, *, kind: str = "name") -> None:
    """Raise ``InvalidKeyError`` when ``name`` violates the english identifier rule.

    Args:
        name: subject to inspect.
        kind: kind label used in the error message (e.g. ``"folder"``, ``"table"``, ``"drawer"``).

    Raises:
        InvalidKeyError: when ``name`` violates the english identifier rule.
    """

    from mddbai.core.errors import InvalidKeyError  # noqa: PLC0415 — avoid circular import

    if is_english_identifier(name):
        return

    suggestion = suggest_english(name)
    raise InvalidKeyError(
        f"invalid {kind}: {name!r} — must be english lowercase "
        f"([a-z0-9][a-z0-9_-]{{0,63}}). suggested: {suggestion!r}",
        kind=kind,
        original=name,
        suggested=suggestion,
    )


__all__ = [
    "ENGLISH_IDENTIFIER_RE",
    "MAX_LENGTH",
    "is_english_identifier",
    "suggest_english",
    "validate_english_identifier",
]

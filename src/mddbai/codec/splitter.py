from __future__ import annotations

"""2026-05-07 — Generic markdown splitter (strict retrieval ingest helper).

Principle alignment:
- D7 Loci — *deciding* how to split (chapter / verse / article) is the AI's / user's
  responsibility. This module handles only *space work* (cutting by structural pattern).
- No domain hard-coding — does not know content types like Bible / legal codes / game design.
  If the caller passes a regex (heading pattern), it is cut as given.
- No semantic extraction — section id comes from the caller-provided ``id_pattern`` (regex capture
  group) or the first line as-is. Auto-embedding of cue / summary is decided by the *caller*
  via ``derive_structural_cue`` or their own cue.

Usage pattern (ingest flow)::

    from mddbai.codec.splitter import split_by_heading

    text = Path("input.md").read_text(encoding="utf-8")
    sections = split_by_heading(
        text,
        heading_regex=r"^##\\s+(.+)$",
        id_capture_group=1,
    )
    # -> [SplitSection(id=..., heading=..., body=..., index=...), ...]
    for s in sections:
        db.put_section(table, drawer, s.id, s.body)

Ingest that embeds the entire body as one ``body`` is not aligned with strict retrieval
(navigate cannot pinpoint the right section and take falls back to whole-file dump). This splitter
is a helper to *block that regression at the ingest stage*.
"""

import re
from dataclasses import dataclass, field
from typing import Iterator


@dataclass(slots=True, frozen=True)
class SplitSection:
    """One section produced by the splitter.

    Attributes:
        id: section id (slug). heading capture group or auto-normalized.
        heading: heading line itself (matched raw line, excluding newline).
        body: body from after the heading until just before the next heading (trimmed front/back).
        index: 0-based order of appearance.
    """

    id: str
    heading: str
    body: str
    index: int


class SplitterError(ValueError):
    """splitter input / regex error."""


_FALLBACK_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def slugify_heading(text: str, *, max_len: int = 64) -> str:
    """heading text -> conservative slug (lowercase ASCII + digits + ``_-``).

    - Korean / CJK is *kept as-is* (mddbai's identifier rule allows multilingual content).
    - whitespace / special chars -> normalized to ``-``. Consecutive ``-`` collapsed to one.
    - if the result is empty, falls back to ``"section"``.
    """

    if not text:
        return "section"
    # newline / tab -> space
    s = re.sub(r"[\s]+", "-", text.strip())
    # remove everything except letters / digits / existing ``-_`` / CJK
    out_chars: list[str] = []
    for ch in s:
        cp = ord(ch)
        if ch.isalnum() or ch in {"-", "_"}:
            out_chars.append(ch.lower() if ch.isascii() else ch)
        elif 0x3000 <= cp <= 0x9FFF or 0xAC00 <= cp <= 0xD7A3:
            # CJK / Hangul syllables — kept as-is
            out_chars.append(ch)
        else:
            out_chars.append("-")
    s = "".join(out_chars)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if not s:
        s = "section"
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "section"


def split_by_heading(
    text: str,
    *,
    heading_regex: str = r"^##\s+(.+)$",
    id_capture_group: int = 1,
    flags: int = re.MULTILINE,
    min_body_chars: int = 0,
) -> list[SplitSection]:
    """Cut source text at lines matching ``heading_regex`` and return a list of ``SplitSection``.

    Args:
        text: source markdown.
        heading_regex: regex that matches heading lines. Default ``^##\\s+(.+)$``
            (markdown H2). For numbering schemes like Bible / legal codes, the caller
            passes their own pattern (e.g. ``r"^##\\s+(\\d+:\\d+)"``). No domain words.
        id_capture_group: index of the capture group from heading match to use as id.
            ``0`` = whole match. ``1`` = first capture group (default).
        flags: ``re`` flags. Default ``MULTILINE``.
        min_body_chars: sections whose body length is below this are *excluded*. 0 = keep all.

    Returns:
        ``list[SplitSection]`` — empty list if no heading matches.

    Raises:
        SplitterError: regex compile failure or ``id_capture_group`` exceeds
            the regex's group count.
    """

    if text is None:
        raise SplitterError("text must not be None")
    try:
        pat = re.compile(heading_regex, flags)
    except re.error as exc:
        raise SplitterError(f"invalid heading_regex: {exc}") from exc
    if id_capture_group < 0:
        raise SplitterError(f"id_capture_group must be >= 0, got {id_capture_group}")
    if id_capture_group > 0 and pat.groups < id_capture_group:
        raise SplitterError(
            f"id_capture_group={id_capture_group} but regex has only {pat.groups} group(s)"
        )

    matches = list(pat.finditer(text))
    if not matches:
        return []

    used_ids: set[str] = set()
    sections: list[SplitSection] = []
    for i, m in enumerate(matches):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip("\n")
        if min_body_chars and len(body.strip()) < min_body_chars:
            continue
        heading_line = m.group(0).rstrip("\r\n")
        if id_capture_group == 0:
            raw_id = m.group(0)
        else:
            raw_id = m.group(id_capture_group) or ""
        sid = slugify_heading(raw_id)
        # On duplicate, suffix ``-2`` ``-3`` (domain-agnostic simple disambiguation)
        unique_sid = sid
        n = 2
        while unique_sid in used_ids:
            unique_sid = f"{sid}-{n}"
            n += 1
        used_ids.add(unique_sid)
        sections.append(
            SplitSection(
                id=unique_sid,
                heading=heading_line,
                body=body,
                index=len(sections),
            )
        )

    return sections


def iter_split_by_heading(
    text: str,
    *,
    heading_regex: str = r"^##\s+(.+)$",
    id_capture_group: int = 1,
    flags: int = re.MULTILINE,
) -> Iterator[SplitSection]:
    """Generator variant of ``split_by_heading``. Used for streaming ingest of very large inputs
    (e.g. entire books). Memory still holds the full source text (no text copy)."""

    yield from split_by_heading(
        text,
        heading_regex=heading_regex,
        id_capture_group=id_capture_group,
        flags=flags,
    )


__all__ = [
    "SplitSection",
    "SplitterError",
    "iter_split_by_heading",
    "slugify_heading",
    "split_by_heading",
]

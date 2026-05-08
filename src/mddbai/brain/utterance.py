from __future__ import annotations

"""K.0.2 / K.1.1 — raw utterance capture + PII masking.

Principle: "MDDB takes *nothing* from the user."

Replace PII with five regexes *before* persisting raw utterances to disk.
Sensitive information can still slip through, so the entry hook itself can
be disabled via ``mddbai hook disable``. Default retention for
``_inbox/utterance/`` is 30 days.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mddbai.codec.frontmatter import parse as _fm_parse
from mddbai.codec.frontmatter import render as _fm_render
from mddbai.core.ulid import new_ulid
from mddbai.storage.atomic import atomic_write_text

# ---------------------------------------------------------------------------
# Five masking patterns
# ---------------------------------------------------------------------------

# Stage N (Gap 22): expanded coverage to 8 PII variants.
# Priority: apply the more *specific* patterns first, to avoid side effects
# such as a 14-digit card being misdetected as a phone.
# Order: card > rrn > email > phone > ipv4.
#
# Per-category variants:
#   email  — standard + Korean (IDN) domains
#   card   — 16 / 15 (Amex) / 14 (Diners) digits + 4-4-4-4 split + 16 contiguous
#   rrn    — formal 6-7 + asterisk-masked variant (e.g. `123456-*******`,
#            7 secret digits)
#   phone  — 11 contiguous digits (e.g. 01012345678) + split forms
#            (010-xxxx-xxxx) + international
#   ipv4   — standard
_MASK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 1) card — first. 14 / 15 / 16 digits.
    #    Amex: 4-6-5 or 15 digits / Diners: 14 digits / Visa, MC: 16 digits
    (
        "card",
        re.compile(
            r"\b(?:"
            r"(?:\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4})"   # 16 split
            r"|(?:\d{4}[-\s]\d{6}[-\s]\d{5})"             # Amex 4-6-5
            r"|(?:\d{4}[-\s]\d{6}[-\s]\d{4})"             # Diners 4-6-4
            r"|\d{16}"                                    # 16 contiguous
            r"|\d{15}"                                    # Amex 15 contiguous
            r"|\d{14}"                                    # Diners 14 contiguous
            r")\b"
        ),
    ),
    # 2) rrn — Korean Resident Registration Number 6-7 (formal) +
    #    asterisk-masked variant.
    #    Asterisks are not word chars, so `\b` does not work; use lookarounds
    #    on both sides instead.
    (
        "rrn",
        re.compile(
            r"(?<![\d\-])"
            r"\d{6}-(?:\d{7}|[\d*]{7}|\*{7})"
            r"(?![\d\-])"
        ),
    ),
    # 3) email — ASCII + IDN (e.g. Korean domains). In Python 3 \w is a
    #    Unicode word character.
    (
        "email",
        re.compile(
            r"(?<![\w.+\-])"
            r"[\w.%+\-]+@[\w\-]+(?:\.[\w\-]+)*\.[\w\-]{2,}"
            r"(?![\w.+\-])"
        ),
    ),
    # 4) phone — split forms (Korean 010-xxxx-xxxx, 02-xxx-xxxx,
    #    international +xx-x-xxx-xxx) + 11 contiguous digits (e.g.
    #    01012345678, Korean mobile)
    (
        "phone",
        re.compile(
            r"(?<!\d)"
            r"(?:"
            # International: +<1-3>-<1-4>-<3-4>-<4>
            r"\+\d{1,3}[-.\s]\d{1,4}[-.\s]\d{3,4}[-.\s]\d{4}"
            # Split: <2-4>-<3-4>-<4>
            r"|(?:\(?\d{2,4}\)?[-.\s])\d{3,4}[-.\s]\d{4}"
            # 11 contiguous digits (Korean mobile starting 010, 011, 016-019)
            r"|01[016789]\d{7,8}"
            r")"
            r"(?!\d)"
        ),
    ),
    # 5) ipv4
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
)


@dataclass(frozen=True, slots=True)
class MaskResult:
    """Masking result: replaced text + counts per kind."""

    text: str
    counts: dict[str, int]

    def total_redactions(self) -> int:
        return sum(self.counts.values())


def mask_pii(text: str) -> MaskResult:
    """Replace the five PII patterns with ``<redacted:kind>``.

    Returns:
        ``MaskResult(text=<masked>, counts={kind: n, ...})``
    """

    counts: dict[str, int] = {}
    out = text
    for kind, pat in _MASK_PATTERNS:
        new, n = pat.subn(f"<redacted:{kind}>", out)
        if n:
            counts[kind] = n
            out = new
    return MaskResult(text=out, counts=counts)


# ---------------------------------------------------------------------------
# Raw utterance capture (writing to disk)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """Utterance capture result."""

    path: Path
    utterance_id: str
    redactions: dict[str, int]


def _ts_to_iso(ts_ns: int) -> str:
    seconds = ts_ns / 1_000_000_000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def capture_utterance(
    data_dir: Path,
    raw_text: str,
    *,
    session_id: str,
    ts_ns: int,
    turn_idx: int | None = None,
    source: str = "user_prompt_submit",
) -> CaptureResult:
    """Mask the raw utterance and save it as ``_inbox/utterance/<ulid>.md``.

    Args:
        data_dir: MDDB data root
        raw_text: Original user utterance
        session_id: Session identifier (Claude Code SESSION_ID or arbitrary)
        ts_ns: Capture time (ns epoch)
        turn_idx: Turn index within the same session (optional)
        source: Capture source (default ``user_prompt_submit``)

    Returns:
        ``CaptureResult(path, utterance_id, redactions)``
    """

    inbox = data_dir / "_inbox" / "utterance"
    inbox.mkdir(parents=True, exist_ok=True)

    masked = mask_pii(raw_text)
    uid = str(new_ulid())
    file_path = inbox / f"{uid}.md"

    fm: dict[str, object] = {
        "id": uid,
        "type": "utterance_raw",
        "session_id": session_id,
        "captured_at": _ts_to_iso(ts_ns),
        "captured_at_ns": ts_ns,
        "source": source,
        "redactions": masked.counts,
        "extracted": False,
    }
    if turn_idx is not None:
        fm["turn_idx"] = turn_idx

    body = (
        "# raw utterance\n\n"
        "<!-- This file is processed by the LexiconExtractTask of the sleep job.\n"
        "     Once extracted: true, it may be deleted by the retention policy. -->\n\n"
        f"{masked.text}\n"
    )
    content = _fm_render(fm, body)
    atomic_write_text(file_path, content)

    return CaptureResult(
        path=file_path, utterance_id=uid, redactions=masked.counts
    )


def list_pending_utterances(data_dir: Path) -> list[Path]:
    """List raw files in ``_inbox/utterance/`` to process (regardless of extracted)."""

    inbox = data_dir / "_inbox" / "utterance"
    if not inbox.is_dir():
        return []
    return sorted(p for p in inbox.glob("*.md") if p.is_file())


def apply_retention(
    data_dir: Path,
    *,
    now_ns: int,
    retention_days: int = 30,
    only_extracted: bool = True,
) -> list[Path]:
    """Delete files that exceed retention. By default only files with
    extracted=True are deleted.

    Args:
        data_dir: MDDB data root
        now_ns: Current time (ns epoch)
        retention_days: Retention window (default 30 days)
        only_extracted: If True, only extracted files are deleted; if False,
            non-extracted files are also deleted.

    Returns:
        List of deleted file paths
    """

    inbox = data_dir / "_inbox" / "utterance"
    if not inbox.is_dir():
        return []

    cutoff_ns = now_ns - retention_days * 86_400 * 1_000_000_000
    deleted: list[Path] = []

    for path in inbox.glob("*.md"):
        try:
            fm, _ = _fm_parse(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        captured_ns = fm.get("captured_at_ns")
        if not isinstance(captured_ns, int):
            continue
        if captured_ns >= cutoff_ns:
            continue
        if only_extracted and not fm.get("extracted", False):
            continue
        try:
            path.unlink()
            deleted.append(path)
        except OSError:
            continue

    return deleted


__all__ = [
    "CaptureResult",
    "MaskResult",
    "apply_retention",
    "capture_utterance",
    "list_pending_utterances",
    "mask_pii",
]

#!/usr/bin/env python3
"""Stop hook — append only the *nutrient lines* from the previous AI response into a drawer.

Aligned with the principles:
- D1 / D2 — only regex heuristics, semantic decisions are the AI's.
- One file consolidation — accumulate in the single drawer `lexicon/ai.md`,
  one turn = one section.

Flow:
1. Read JSON from stdin (utf-8) — transcript_path + cwd + session_id
2. If cwd / .mddbai/ is missing, exit silently
3. Read only the *last assistant turn* from the transcript jsonl
4. nutrient_extract.extract_ai_nutrient_lines() — nutrient lines (max 7)
5. mddbai Database.put_section("lexicon", "ai", "t<ts>", body=...)
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from pathlib import Path

MAX_TURN_BODY_BYTES = 4096


def _find_data_dir(cwd: str) -> Path | None:
    candidate = Path(cwd) / ".mddbai"
    return candidate if candidate.is_dir() else None


def _ensure_utf8_stdin() -> None:
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        with contextlib.suppress(Exception):
            sys.stdin = io.TextIOWrapper(
                sys.stdin.buffer, encoding="utf-8", errors="replace"
            )


def _read_last_assistant_turn(transcript_path: str) -> str:
    """Extract just the text of the last assistant turn from the transcript jsonl."""

    transcript = Path(transcript_path) if transcript_path else None
    if transcript is None or not transcript.is_file():
        return ""

    last_text = ""
    try:
        with transcript.open(encoding="utf-8") as fh:
            for raw_line in fh:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                msg = obj.get("message") or obj
                role = msg.get("role") if isinstance(msg, dict) else None
                if role != "assistant":
                    continue
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, list):
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "text"
                        ):
                            text_part = str(block.get("text", ""))
                            if text_part.strip():
                                last_text = text_part
                elif isinstance(content, str) and content.strip():
                    last_text = content
    except OSError:
        return ""

    return last_text


def _previous_section_ref(
    data_path: Path, drawer: str
) -> str | None:
    """Reference to the previous section in the same drawer."""

    try:
        from mddbai.codec.frontmatter import parse as _fm_parse  # noqa: PLC0415

        path = data_path / "lexicon" / f"{drawer}.md"
        if not path.is_file():
            return None
        fm, _ = _fm_parse(path.read_text(encoding="utf-8"))
        sections = fm.get("_sections") or []
        if not sections:
            return None
        last_sid = sections[-1][0] if isinstance(sections[-1], list) else None
        if not last_sid:
            return None
        return f"lexicon/{drawer}#{last_sid}"
    except Exception:
        return None


def _build_structured_body(
    lines: list[tuple[str, str, float]], session_id: str, turn_kind: str
) -> str:
    """Aligned with mddbai-write step 3 — Current / Why / Source H2."""

    parts: list[str] = []

    # ### H3 slot — H2 slots would be parsed as separate sections by mddbai. Keep 1 turn = 1 section.

    parts.append("### Current")
    parts.append("")
    for line, kind, weight in lines:
        parts.append(f"- ({kind}, w={weight:.1f}) {line}")
    parts.append("")

    top = max(lines, key=lambda t: t[2])
    parts.append("### Why")
    parts.append("")
    parts.append(
        f"hook auto-write — {len(lines)} nutrient lines. top: \"{top[0][:80]}\""
    )
    parts.append("")

    parts.append("### Source")
    parts.append("")
    parts.append(f"- kind: {turn_kind}")
    parts.append(f"- session_id: {session_id}")
    parts.append(f"- captured_at_ns: {time.time_ns()}")

    return "\n".join(parts)


def _recall_check(
    data_path: Path,
    drawer: str,
    section_id: str,
    cue_keywords: list[str],
) -> str:
    """Aligned with mddbai-write step 6 — self-recall right after write."""

    if not cue_keywords:
        return "skipped"

    try:
        from mddbai.core.config import MddbConfig  # noqa: PLC0415
        from mddbai.engine import Database  # noqa: PLC0415
    except Exception:
        return "skipped"

    try:
        cfg = MddbConfig(data_dir=data_path)
        db = Database(data_path, config=cfg)
    except Exception:
        return "skipped"

    try:
        cue = cue_keywords[0]
        result = db.navigate(cue, max_routes=3, max_drawers=10, max_sections=20)
        routes = result.get("routes") or []
        for i, r in enumerate(routes[:3]):
            if (
                r.get("table") == "lexicon"
                and r.get("drawer") == drawer
                and r.get("section") == section_id
            ):
                return "ok" if i == 0 else "weak"
        return "miss"
    except Exception:
        return "skipped"
    finally:
        with contextlib.suppress(Exception):
            db.close()


def _put_lexicon_section(
    data_path: Path,
    drawer: str,
    section_id: str,
    lines: list[tuple[str, str, float]],
    session_id: str,
    cue_keywords: list[str],
    turn_kind: str = "ai-response",
) -> str:
    """Aligned with the 6 steps of mddbai-write — Current/Why/Source + cue/aliases/related + recall-check."""

    if not lines:
        return "skipped"

    try:
        from mddbai.core.config import MddbConfig  # noqa: PLC0415
        from mddbai.engine import Database  # noqa: PLC0415
    except Exception:
        return "skipped"

    body = _build_structured_body(lines, session_id, turn_kind)
    if len(body.encode("utf-8")) > MAX_TURN_BODY_BYTES:
        body = body.encode("utf-8")[:MAX_TURN_BODY_BYTES].decode(
            "utf-8", errors="replace"
        )

    # cue / aliases — preserve frequency desc order (no language sorting). The AI fills the gap by inference on recall.
    kind_set = sorted({kind for _line, kind, _w in lines})
    cue = list(cue_keywords) + [k for k in kind_set if k not in cue_keywords]
    aliases = list(cue_keywords)

    top_line = max(lines, key=lambda t: t[2])
    chosen_because = top_line[0]
    confidence = top_line[2]

    related: list[str] = []
    prev_ref = _previous_section_ref(data_path, drawer)
    if prev_ref:
        related.append(prev_ref)

    try:
        cfg = MddbConfig(data_dir=data_path)
        db = Database(data_path, config=cfg)
    except Exception:
        return "skipped"

    # Auto-detect 1-letter lang hint
    try:
        from mddbai.codec.section_meta import detect_lang  # noqa: PLC0415
        lang = detect_lang(chosen_because, *cue, *aliases) or None
    except Exception:
        lang = None

    try:
        db.put_section("lexicon", drawer, section_id, body, fsync=True)
        db.put_section_meta(
            "lexicon",
            drawer,
            section_id,
            cue=cue,
            aliases=aliases or None,
            chosen_because=chosen_because,
            related=related or None,
            state="active",
            confidence=confidence,
            memory_zone="warm",
            lang=lang,
            merge=True,
        )
        db.flush()
    except Exception:
        return "skipped"
    finally:
        with contextlib.suppress(Exception):
            db.close()

    return _recall_check(data_path, drawer, section_id, cue_keywords)


def main() -> int:
    _ensure_utf8_stdin()
    with contextlib.suppress(Exception):
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    cwd = str(payload.get("cwd") or Path.cwd())
    transcript_path = str(payload.get("transcript_path", "") or "")
    session_id = str(payload.get("session_id", "unknown")) or "unknown"

    data_path = _find_data_dir(cwd)
    if data_path is None:
        return 0

    last_text = _read_last_assistant_turn(transcript_path)
    if not last_text or len(last_text.strip()) < 20:
        return 0

    sys.path.insert(0, str(Path(cwd) / "src"))
    try:
        from mddbai.brain.nutrient_extract import (  # noqa: PLC0415
            extract_ai_nutrient_lines,
            extract_cue_keywords,
        )
    except Exception:
        return 0

    nutrient_lines = extract_ai_nutrient_lines(last_text)
    if not nutrient_lines:
        return 0

    section_id = f"t-{time.strftime('%Y%m%dT%H%M%S')}-{session_id[:6]}"
    triples = [(n.line, n.kind, n.weight) for n in nutrient_lines]
    cue_keywords = extract_cue_keywords([n.line for n in nutrient_lines])
    _put_lexicon_section(
        data_path, "ai", section_id, triples, session_id,
        cue_keywords, turn_kind="ai-response",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())

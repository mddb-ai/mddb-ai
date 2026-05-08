#!/usr/bin/env python3
"""UserPromptSubmit hook — append only the *nutrient lines* from the user prompt into a drawer.

Aligned with the principles:
- D1 (no search) — only regex + signal-word heuristics.
- D2 (semantic decisions = AI) — decomposition + writing is a spatial operation.
- One file consolidation — one turn = one section, accumulated in one drawer
  (`lexicon/user.md`). The "one word = one file" model is dropped.

Flow:
1. Read JSON from stdin (utf-8) — prompt + cwd + session_id
2. If cwd / .mddbai/ is missing, exit silently
3. nutrient_extract.extract_user_nutrient_lines() — nutrient lines (max 5)
4. mddbai Database.put_section("lexicon", "user", "t<ts>", body=...)
5. Pass through silently (do not block the user response)

Related:
- ``src/mddbai/brain/nutrient_extract.py`` — nutrient heuristics (line level)
- ``src/mddbai/engine.py`` — Database.put_section
- ``.claude/skills/mddbai-recall/SKILL.md`` — tracing cues from the lexicon drawer
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from pathlib import Path

MAX_TURN_BODY_BYTES = 4096  # per-turn body cap (5 lines x ~200 chars + frontmatter slack)


def _find_data_dir(cwd: str) -> Path | None:
    candidate = Path(cwd) / ".mddbai"
    return candidate if candidate.is_dir() else None


def _ensure_utf8_stdin() -> None:
    """Prevent Korean garbling under Windows default cp949 — force stdin to utf-8."""

    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        # When reconfigure is unavailable — wrap at the buffer level
        with contextlib.suppress(Exception):
            sys.stdin = io.TextIOWrapper(
                sys.stdin.buffer, encoding="utf-8", errors="replace"
            )


def _previous_section_ref(
    data_path: Path, drawer: str
) -> str | None:
    """Reference to the previous section in the same drawer — link slot (Hebbian adjacency)."""

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
    """Aligned with mddbai-write step 3 (Semantic Structuring) — Current / Why / Source.

    Split per H2 unit. Clearly visible to humans / AI when they cat the result.
    """

    parts: list[str] = []

    # Use ### H3 — H2 (##) is parsed by mddbai as a separate section.
    # To keep 1 turn = 1 section, use H3.

    # Current — the nutrient lines themselves
    parts.append("### Current")
    parts.append("")
    for line, kind, weight in lines:
        parts.append(f"- ({kind}, w={weight:.1f}) {line}")
    parts.append("")

    # Why — reason the hook wrote this
    top = max(lines, key=lambda t: t[2])
    parts.append("### Why")
    parts.append("")
    parts.append(
        f"hook auto-write — {len(lines)} nutrient lines. top: \"{top[0][:80]}\""
    )
    parts.append("")

    # Source — kind / session / time
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
    """Aligned with mddbai-write step 6 (Future Recall Check) — self-recall right after write.

    Returns:
        ``"ok"`` | ``"weak"`` | ``"miss"`` | ``"skipped"``.
    """

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
        # Self-test with the single richest cue
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
    turn_kind: str = "user-utterance",
) -> str:
    """Write one section into the drawer — *aligned with the 6 steps of mddbai-write*.

    Step 1 Write Intent: decide ``turn_kind``.
    Step 2 Placement: fixed at lexicon/<drawer> (turn-level location).
    Step 3 Semantic Structuring: H2 (Current / Why / Source).
    Step 4 Metadata: cue / aliases / chosen_because + state / confidence.
    Step 5 Relationship Linking: related = ref to previous turn.
    Step 6 Future Recall Check: navigate self-test.

    Returns:
        recall-check result (``ok`` / ``weak`` / ``miss`` / ``skipped``).
    """

    if not lines:
        return "skipped"

    try:
        from mddbai.core.config import MddbConfig  # noqa: PLC0415
        from mddbai.engine import Database  # noqa: PLC0415
    except Exception:
        return "skipped"

    # Step 3 Semantic Structuring — body split by H2
    body = _build_structured_body(lines, session_id, turn_kind)
    if len(body.encode("utf-8")) > MAX_TURN_BODY_BYTES:
        body = body.encode("utf-8")[:MAX_TURN_BODY_BYTES].decode(
            "utf-8", errors="replace"
        )

    # Step 4 Metadata — cue / aliases / chosen_because
    # cue = preserve frequency desc order (no language sorting). On recall the AI
    # fills in other languages by *semantic inference* (aligned with mddbai-recall §6.1 (c)).
    kind_set = sorted({kind for _line, kind, _w in lines})
    cue = list(cue_keywords) + [k for k in kind_set if k not in cue_keywords]
    aliases = list(cue_keywords)  # aliases share the same source as cue

    top_line = max(lines, key=lambda t: t[2])
    chosen_because = top_line[0]
    confidence = top_line[2]

    # Step 5 Relationship Linking — ref to previous turn
    related: list[str] = []
    prev_ref = _previous_section_ref(data_path, drawer)
    if prev_ref:
        related.append(prev_ref)

    try:
        cfg = MddbConfig(data_dir=data_path)
        db = Database(data_path, config=cfg)
    except Exception:
        return "skipped"

    # Auto-detect 1-letter lang hint (section_meta.detect_lang)
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

    # Step 6 Future Recall Check
    return _recall_check(data_path, drawer, section_id, cue_keywords)


def main() -> int:
    _ensure_utf8_stdin()
    with contextlib.suppress(Exception):
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    prompt = str(payload.get("prompt", "") or "")
    cwd = str(payload.get("cwd") or Path.cwd())
    session_id = str(payload.get("session_id", "unknown")) or "unknown"

    if not prompt or len(prompt.strip()) < 8:
        return 0

    data_path = _find_data_dir(cwd)
    if data_path is None:
        return 0

    try:
        from mddbai.brain.nutrient_extract import (  # noqa: PLC0415
            extract_cue_keywords,
            extract_user_nutrient_lines,
        )
    except Exception:
        return 0

    nutrient_lines = extract_user_nutrient_lines(prompt)
    if not nutrient_lines:
        return 0

    section_id = f"t-{time.strftime('%Y%m%dT%H%M%S')}-{session_id[:6]}"
    triples = [(n.line, n.kind, n.weight) for n in nutrient_lines]
    cue_keywords = extract_cue_keywords([n.line for n in nutrient_lines])
    _put_lexicon_section(
        data_path, "user", section_id, triples, session_id,
        cue_keywords, turn_kind="user-utterance",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())

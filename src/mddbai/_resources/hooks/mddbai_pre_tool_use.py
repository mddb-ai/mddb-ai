#!/usr/bin/env python3
"""PreToolUse hook — guidance just before Grep / Bash(rg/grep/findstr) calls.

Claude Code's PreToolUse hook. Just before a tool call, the hook receives
a JSON payload via stdin (tool_name + tool_input). When the search pattern
targets inside .mddbai/, it prints guidance to stderr and records
tutorial.record_call(via_door=False) statistics.

Uses blocking (exit 2). User decision (2026-05-07):
"let's enforce it, just within Claude Code for now". The exact enforcement
strength is a follow-up decision.

Related:
- `.claude/rules/no-grep-escape.md`
- `.claude/skills/mddbai-recall/SKILL.md`
"""
from __future__ import annotations

import json
import os
import re
import sys


# Bypass patterns that search inside mddbai via Bash commands
_BASH_ESCAPE_PATTERN = re.compile(
    r"\b(rg|grep|findstr|select-string|sls|cat|type)\b.*\.mddbai",
    re.IGNORECASE,
)


def _has_mddbai_target(text: str) -> bool:
    """Whether the search target path contains .mddbai/."""
    return ".mddbai" in (text or "")


def _is_escape(tool_name: str, tool_input: dict) -> tuple[bool, str]:
    """Whether this is a bypass pattern — (yes, reason)."""
    if tool_name == "Grep":
        path = str(tool_input.get("path", "") or "")
        # Even without an explicit path, .mddbai under cwd is possible — only flag the explicit case for now
        if _has_mddbai_target(path):
            return True, f"Grep call (path={path}). Prefer mddbai read first."
        # No explicit path + .mddbai exists under cwd -> potential bypass
        if not path and os.path.isdir(os.path.join(os.getcwd(), ".mddbai")):
            pattern = str(tool_input.get("pattern", ""))
            return True, f"Grep call (cwd-based, pattern={pattern!r}). mddbai read first."
        return False, ""

    if tool_name == "Read":
        file_path = str(tool_input.get("file_path", ""))
        if _has_mddbai_target(file_path) and file_path.endswith(".md"):
            return True, f"Whole-file Read call ({file_path}). Prefer mddbai take --body-only."
        return False, ""

    if tool_name == "Glob":
        pattern = str(tool_input.get("pattern", ""))
        if _has_mddbai_target(pattern):
            return True, f"Glob call ({pattern}). Prefer mddbai list-drawers."
        return False, ""

    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        if _BASH_ESCAPE_PATTERN.search(cmd):
            return True, "Bash rg/grep/findstr/select-string/cat/type targeting .mddbai. Prefer mddbai cues / navigate."
        return False, ""

    return False, ""


def _record_back_door(cwd: str, tool_name: str) -> None:
    """Record tutorial.record_call(via_door=False) statistics. Silent on failure."""
    mddbai_dir = os.path.join(cwd, ".mddbai")
    if not os.path.isdir(mddbai_dir):
        return
    try:
        import subprocess  # noqa: PLC0415
        subprocess.run(
            [
                sys.executable, "-c",
                (
                    "from mddbai.brain.tutorial import record_call;"
                    f"record_call(r'{mddbai_dir}', cmd={tool_name!r}, via_door=False)"
                ),
            ],
            timeout=5,
            capture_output=True,
        )
    except Exception:
        pass


def main() -> int:
    """PreToolUse hook entry."""
    # Windows utf-8 safety
    try:
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # parse failure — pass silently

    tool_name = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input", {}) or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    is_esc, reason = _is_escape(tool_name, tool_input)
    if not is_esc:
        return 0

    cwd = payload.get("cwd") or os.getcwd()

    # stderr guidance + actual block (exit 2)
    print(
        f"[mddbai-recall blocked] {reason}\n"
        f"\n"
        f"Default order (no bypass — inference step):\n"
        f"  1. mddbai read .mddbai \"<cue>\"\n"
        f"  2. No result -> dump the result of mddbai list-sections / cues\n"
        f"  3. *Inference* — semantic mapping between Korean cue and English section slug\n"
        f"     e.g. 'first act' (KR: 1mak) -> 'PreparationPhase' / 'Phase1'\n"
        f"  4. mddbai take .mddbai <table> <drawer> <picked-section>\n"
        f"\n"
        f"Rule: .claude/rules/no-grep-escape.md\n"
        f"Skill: .claude/skills/mddbai-recall/SKILL.md\n",
        file=sys.stderr,
    )

    # Record statistics
    _record_back_door(cwd, tool_name)

    # exit 2 = blocking error. Claude Code will reject the tool call.
    return 2


if __name__ == "__main__":
    sys.exit(main())

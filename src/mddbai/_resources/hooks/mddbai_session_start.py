#!/usr/bin/env python3
"""SessionStart hook — inject mddbai-native flow rule guidance.

Slot for Claude Code's SessionStart hook. This script is invoked at the
start of every session. stdout adds system context (Claude receives it
before its first response).

Related:
- `.claude/rules/no-grep-escape.md` — main rule
- `.claude/skills/mddbai-recall/SKILL.md` — auto-trigger skill

Conditions:
- Only inject the guidance when a `.mddbai/` folder exists in the
  current cwd
- Otherwise stay silent (the user is not using mddbai here)
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    """SessionStart hook entry."""
    # Only when .mddbai exists inside cwd
    cwd = os.getcwd()
    mddbai_dir = os.path.join(cwd, ".mddbai")
    if not os.path.isdir(mddbai_dir):
        return 0

    # Windows utf-8 safety
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass

    msg = (
        "[mddbai-recall] This project uses a .mddbai/ palace. Default recall order:\n"
        "  1. mddbai read .mddbai \"<cue>\"  (first call)\n"
        "  2. No result -> reinforce 1~2 of the 4 hint dimensions (time/place/people/sense) -> retry\n"
        "  3. Still no result after 3 reinforcements -> mddbai cues / mddbai navigate\n"
        "  4. Still no result -> acknowledge *information not present*. Do NOT bypass via Grep / whole-file Read / cat.\n"
        "\n"
        "* Self-check just before calling Grep / Bash(rg/grep/findstr):\n"
        "  - Did you call mddbai read first? Did you try reinforcing the 4 hint dimensions? Did you check cues/navigate?\n"
        "* Main rule: .claude/rules/no-grep-escape.md\n"
        "* Skill: .claude/skills/mddbai-recall/SKILL.md\n"
    )
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())

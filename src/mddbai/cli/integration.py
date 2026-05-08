from __future__ import annotations

"""J.1 — Integration policy code (hook installation + rule-file snippet).

Principle: "MDDB does not invade the user's environment."

- ``mddbai init`` (no options): only creates an on-disk guide; prints a
  per-tool copy-paste matrix to the console.
- ``mddbai init --with-claude-hook``: auto-installs Claude Code's SessionStart
  hook (real enforcement only when the platform provides a real mechanism).
- ``mddbai init --write-rules-snippet=<tool>``: only when the user explicitly
  requests it; writes an idempotent (marker-based) snippet into per-tool
  rule files.
"""

import contextlib
import json
import shutil
import sys
from importlib.resources import files
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Python hook + skill installation (mddbai-native)
# ---------------------------------------------------------------------------
#
# Embeds Python hooks at Claude Code's 4 hook points (SessionStart /
# PreToolUse / UserPromptSubmit / Stop). Separate from the existing bash
# hooks, this supports the mddbai-native flow (6-step recall + nutrient
# extraction).
#

_PY_HOOK_FILES = (
    "mddbai_session_start.py",
    "mddbai_pre_tool_use.py",
    "mddbai_user_prompt_submit.py",
    "mddbai_stop.py",
)

_PY_HOOK_EVENTS: tuple[tuple[str, str], ...] = (
    ("SessionStart", "mddbai_session_start.py"),
    ("PreToolUse", "mddbai_pre_tool_use.py"),
    ("UserPromptSubmit", "mddbai_user_prompt_submit.py"),
    ("Stop", "mddbai_stop.py"),
)

_SKILL_NAMES = (
    "mddbai-write",
    "mddbai-recall",
    "mddbai-consolidate",
)


def _resolve_python_for_hooks(project_root: Path) -> str:
    """Determine the python path to write into settings.json.

    - Isolated install (``sys.executable`` inside project_root's .venv) -> relative path (portable)
    - Global install (``sys.executable`` outside project_root) -> ``python`` (PATH-dependent)
    """

    exe = Path(sys.executable).resolve()
    try:
        rel = exe.relative_to(project_root.resolve())
        return rel.as_posix()
    except ValueError:
        return "python"


def _py_hook_command(hook_filename: str, project_root: Path) -> str:
    """Hook invocation command to write into settings.json.

    For an isolated install (.mddbai/.venv/), use that venv's python via a
    relative path. For a global install, fall back to ``python`` (PATH-dependent).
    """

    py = _resolve_python_for_hooks(project_root)
    if " " in py:
        return f'"{py}" .claude/hooks/{hook_filename}'
    return f"{py} .claude/hooks/{hook_filename}"


def _resource_skills_root():  # pragma: no cover - importlib.resources behavior
    return files("mddbai._resources") / "skills"


def _resource_hooks_root():  # pragma: no cover
    return files("mddbai._resources") / "hooks"


def install_python_hooks(project_root: Path) -> list[Path]:
    """Copy the 4 ``_resources/hooks/*.py`` files into the user's ``.claude/hooks/``.

    Overwrites files with the same name (so mddbai package updates take effect).

    Returns:
        List of installed hook file paths.
    """

    hooks_dir = project_root / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    src_root = _resource_hooks_root()
    installed: list[Path] = []
    for name in _PY_HOOK_FILES:
        src = src_root / name
        dst = hooks_dir / name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        with contextlib.suppress(PermissionError, NotImplementedError):
            dst.chmod(0o755)
        installed.append(dst)
    return installed


def install_mddbai_skills(project_root: Path) -> list[Path]:
    """Copy the 3 ``_resources/skills/<name>/SKILL.md`` files into the user's ``.claude/skills/``.

    Returns:
        List of installed SKILL.md file paths.
    """

    skills_dir = project_root / ".claude" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    src_root = _resource_skills_root()
    installed: list[Path] = []
    for name in _SKILL_NAMES:
        src = src_root / name / "SKILL.md"
        dst_dir = skills_dir / name
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / "SKILL.md"
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        installed.append(dst)
    return installed


def register_python_hooks_in_settings(
    project_root: Path,
) -> tuple[Path, Literal["created", "updated", "already"]]:
    """Register Python hook commands into the 4 hook arrays in ``.claude/settings.json``.

    If the same command already exists, no append. Otherwise append.
    """

    settings_path = project_root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    desired: list[tuple[str, str]] = [
        (event, _py_hook_command(filename, project_root))
        for event, filename in _PY_HOOK_EVENTS
    ]

    if not settings_path.exists():
        settings_init: dict[str, object] = {"hooks": {}}
        for event, cmd in desired:
            settings_init["hooks"][event] = [  # type: ignore[index]
                {"hooks": [{"type": "command", "command": cmd}]}
            ]
        settings_path.write_text(
            json.dumps(settings_init, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return settings_path, "created"

    raw = settings_path.read_text(encoding="utf-8")
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"failed to parse {settings_path}: {exc}. fix the JSON manually first."
        ) from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{settings_path}: top-level must be JSON object")
    settings_obj: dict[str, object] = loaded

    hooks_root_obj = settings_obj.setdefault("hooks", {})
    if not isinstance(hooks_root_obj, dict):
        raise RuntimeError(f"{settings_path}: 'hooks' must be JSON object")
    hooks_root: dict[str, object] = hooks_root_obj

    any_added = False
    for event, cmd in desired:
        added = _ensure_hook_command(hooks_root, event, cmd)
        any_added = any_added or added

    if not any_added:
        return settings_path, "already"

    settings_path.write_text(
        json.dumps(settings_obj, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return settings_path, "updated"


def install_claude_full(
    project_root: Path,
) -> dict[str, object]:
    """Install the full Claude Code integration (mddbai-native).

    1. Copy 4 Python hooks (``.claude/hooks/mddbai_*.py``)
    2. Copy 3 skills (``.claude/skills/<name>/SKILL.md``)
    3. Register hook commands in the 4 slots of ``.claude/settings.json``
       (SessionStart / PreToolUse / UserPromptSubmit / Stop)

    Returns:
        ``{"hooks": [Path], "skills": [Path], "settings": Path,
           "settings_status": "created"|"updated"|"already"}``
    """

    hooks = install_python_hooks(project_root)
    skills = install_mddbai_skills(project_root)
    settings_path, status = register_python_hooks_in_settings(project_root)
    return {
        "hooks": hooks,
        "skills": skills,
        "settings": settings_path,
        "settings_status": status,
    }


def uninstall_claude_full(
    project_root: Path,
    *,
    delete_files: bool = False,
) -> dict[str, object]:
    """Uninstall the full Claude Code integration.

    - Remove the 4 hook commands from ``settings.json`` (other commands preserved)
    - When ``delete_files=True``, also delete ``.claude/hooks/mddbai_*.py`` and ``.claude/skills/mddbai-*/SKILL.md``

    Returns:
        ``{"removed_count": int, "settings": Path|None}``
    """

    settings_path = project_root / ".claude" / "settings.json"
    removed_count = 0

    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            hooks_root_obj = loaded.get("hooks")
            if isinstance(hooks_root_obj, dict):
                for event, filename in _PY_HOOK_EVENTS:
                    cmd = _py_hook_command(filename, project_root)
                    bucket = hooks_root_obj.get(event)
                    if not isinstance(bucket, list):
                        continue
                    new_bucket: list[object] = []
                    for entry in bucket:
                        if isinstance(entry, dict):
                            inner = entry.get("hooks", [])
                            if isinstance(inner, list):
                                kept = [
                                    h
                                    for h in inner
                                    if not (
                                        isinstance(h, dict) and h.get("command") == cmd
                                    )
                                ]
                                if len(kept) != len(inner):
                                    removed_count += 1
                                if kept:
                                    new_entry = dict(entry)
                                    new_entry["hooks"] = kept
                                    new_bucket.append(new_entry)
                                continue
                        new_bucket.append(entry)
                    if new_bucket:
                        hooks_root_obj[event] = new_bucket
                    else:
                        del hooks_root_obj[event]
                if not hooks_root_obj:
                    del loaded["hooks"]
                settings_path.write_text(
                    json.dumps(loaded, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )

    if delete_files:
        hooks_dir = project_root / ".claude" / "hooks"
        for name in _PY_HOOK_FILES:
            p = hooks_dir / name
            if p.exists():
                with contextlib.suppress(OSError):
                    p.unlink()
        skills_dir = project_root / ".claude" / "skills"
        for name in _SKILL_NAMES:
            d = skills_dir / name
            if d.exists():
                with contextlib.suppress(OSError):
                    shutil.rmtree(d)

    return {
        "removed_count": removed_count,
        "settings": settings_path if settings_path.exists() else None,
    }


def claude_full_status(project_root: Path) -> dict[str, object]:
    """Install state of the 4 Python hooks + 3 skills."""

    hooks_dir = project_root / ".claude" / "hooks"
    skills_dir = project_root / ".claude" / "skills"
    settings_path = project_root / ".claude" / "settings.json"

    hooks_present = {
        name: (hooks_dir / name).exists() for name in _PY_HOOK_FILES
    }
    skills_present = {
        name: (skills_dir / name / "SKILL.md").exists() for name in _SKILL_NAMES
    }

    in_settings: dict[str, bool] = {}
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = None
        for event, filename in _PY_HOOK_EVENTS:
            in_settings[event] = _command_in_settings(
                loaded, event, _py_hook_command(filename, project_root)
            )
    else:
        for event, _ in _PY_HOOK_EVENTS:
            in_settings[event] = False

    all_installed = (
        all(hooks_present.values())
        and all(skills_present.values())
        and all(in_settings.values())
    )
    return {
        "installed": all_installed,
        "hooks": hooks_present,
        "skills": skills_present,
        "in_settings": in_settings,
        "settings": settings_path if settings_path.exists() else None,
    }

# ---------------------------------------------------------------------------
# Tool -> rule-file mapping
# ---------------------------------------------------------------------------

#
# Locations chosen per each CLI's *modern best practice* (J.1 update):
#
# - claude  : ``CLAUDE.md``                  — user rules (shared file, marker-based)
# - cursor  : ``.cursor/rules/mddbai.mdc``   — Project Rules (dedicated file, frontmatter)
# - gemini  : ``GEMINI.md``                  — user rules (shared file, marker-based)
# - codex   : ``AGENTS.md``                  — user rules (shared file, marker-based)
#
# Cursor's modern ``.mdc`` is a *file we own*, so we rewrite the whole file
# idempotently instead of using markers. The frontmatter must be present so
# alwaysApply works.

TOOL_RULE_FILE: dict[str, str] = {
    "claude": "CLAUDE.md",
    "cursor": ".cursor/rules/mddbai.mdc",
    "gemini": "GEMINI.md",
    "codex": "AGENTS.md",
}

# Tools where mddbai *owns the whole file* (e.g. ``.cursor/rules/mddbai.mdc``).
# These are rewritten in full without markers.
_DEDICATED_FILE_TOOLS: frozenset[str] = frozenset({"cursor"})

ToolName = Literal["claude", "cursor", "gemini", "codex"]
SUPPORTED_TOOLS: tuple[str, ...] = ("claude", "cursor", "gemini", "codex")

# Markers — the block between these two lines is owned by mddbai (for idempotent updates).
MDDB_MARKER_START = "<!-- mddbai:start -->"
MDDB_MARKER_END = "<!-- mddbai:end -->"


# ---------------------------------------------------------------------------
# Rule snippet (body is identical across tools; only location/marker differs)
# ---------------------------------------------------------------------------

_SNIPPET_BODY = """\
## MDDB data (mddbai.ai)

This project manages data via [mddbai.ai](https://github.com/mddbai/mddbai).

- **data root**: `{data_rel}/`
- **on first entry**: read `{data_rel}/_AGENT_GUIDE.md` once. The disk holds
  the multi-stage navigation/write protocol (6 steps) plus a command bundle.

**Core motto**: `Map first. Lexical cue fallback only when necessary. Exact section read only.`
MDDBAI is NOT a vector DB / embedding retrieval / semantic search engine.
Search is not the *driver* — it provides only the *space* for markdown
topology + lexical cue routing.

### Reading — 6-step multi-stage protocol

| step | who | tool |
|---|---|---|
| 1. Query Intent Analysis | you (AI) | (own reasoning) |
| 2. Memory Route Planning | you | `_palace.md`, `_summary.md` |
| 3. Parallel Navigation | MDDBAI | `mddbai map {data_rel} "<cue>"` (drawer body 0 byte; `_summary.md` <=4KB) |
| 4. Evidence Reading | MDDBAI | `mddbai recall {data_rel} "<cue>" --strict` or `take` |
| 5. Meaning Reconstruction | you | `--include-superseded`, recall stderr `# conflict:` signals |
| 6. User Output Reconstruction | you | (own synthesis) |

**Forbidden (raw-scan regression):** Do NOT whole-file `Read` large .md files
inside `{data_rel}/` to find answers. Do NOT grep bodies via `Grep` /
`Select-String` / `sed`. Do NOT use `mddbai take <table> <drawer>` (no section)
to dump a whole drawer. If you see "smallest unit too large / single body
only", do not dump body — split/enrich finer with `mddbai ingest-document`.

### Writing — 6-step multi-stage protocol (only when the user *explicitly* asks)

Save only when the user uses an **explicit trigger** like "make a note /
save / record / write down / leave / add a task". Don't autosave every
turn of the conversation.

| step | tool |
|---|---|
| 1. Write Intent | (AI itself) |
| 2. Placement | reuse an existing drawer via `mddbai cues {data_rel}` / `list-drawers` |
| 3. Semantic Structuring | (AI shapes the body: Current / Why / Evidence / Related / Revision) |
| 4. Metadata Enrichment | `mddbai put-section ... --cue --entity --date --source --confidence --memory-zone --state --current-revision --supersedes` |
| 5. Relationship Linking | `mddbai link {data_rel} <a-ref> <b-ref>` (bidirectional by default) |
| 6. Future Recall Check | `put-section ... --recall-check` (self-test right after writing) |

Body can also come from stdin / file (`--body-stdin`, `--body-file`). For
detailed conventions, see `_AGENT_GUIDE.md`.

### Health check

`mddbai doctor {data_rel}` — reports missing summaries / broken links /
branching blowup in one shot.
"""


def _build_snippet(data_rel: str) -> str:
    body = _SNIPPET_BODY.format(data_rel=data_rel)
    return f"{MDDB_MARKER_START}\n{body}{MDDB_MARKER_END}\n"


# Cursor modern Project Rules dedicated file — frontmatter + marker body
_CURSOR_MDC_TEMPLATE = """\
---
description: MDDB memory location and conventions for this project
globs:
  - "**/*"
alwaysApply: true
---

{snippet}"""


def _build_cursor_mdc(data_rel: str) -> str:
    return _CURSOR_MDC_TEMPLATE.format(snippet=_build_snippet(data_rel))


# ---------------------------------------------------------------------------
# Write rule snippet (idempotent)
# ---------------------------------------------------------------------------


def _write_dedicated_file(
    project_root: Path,
    tool: str,
    data_rel: str,
) -> tuple[Path, str]:
    """Idempotently rewrite the entire dedicated file (Cursor .mdc)."""

    rule_file = project_root / TOOL_RULE_FILE[tool]
    rule_file.parent.mkdir(parents=True, exist_ok=True)
    if tool == "cursor":
        content = _build_cursor_mdc(data_rel)
    else:  # pragma: no cover - currently only cursor
        raise NotImplementedError(f"dedicated tool {tool!r} unhandled")

    if not rule_file.exists():
        rule_file.write_text(content, encoding="utf-8")
        return rule_file, "created"
    if rule_file.read_text(encoding="utf-8") == content:
        return rule_file, "already"
    rule_file.write_text(content, encoding="utf-8")
    return rule_file, "updated"


def write_rules_snippet(
    project_root: Path,
    tool: str,
    data_rel: str,
) -> tuple[Path, str]:
    """Write an idempotent mddbai snippet into ``tool``'s rule file.

    Args:
        project_root: Project root where the rule file lives
        tool: ``claude`` | ``cursor`` | ``gemini`` | ``codex``
        data_rel: POSIX path of the mddbai data directory relative to the rule file

    Returns:
        ``(rule_file_path, status)`` — status in ``"created"`` |
        ``"updated"`` | ``"appended"`` | ``"already"``
    """

    if tool not in TOOL_RULE_FILE:
        raise ValueError(f"unknown tool: {tool!r}. supported: {SUPPORTED_TOOLS}")

    # Dedicated-file tools (Cursor .mdc) take a separate path
    if tool in _DEDICATED_FILE_TOOLS:
        return _write_dedicated_file(project_root, tool, data_rel)

    rule_file = project_root / TOOL_RULE_FILE[tool]
    snippet = _build_snippet(data_rel)

    if not rule_file.exists():
        rule_file.parent.mkdir(parents=True, exist_ok=True)
        rule_file.write_text(snippet, encoding="utf-8")
        return rule_file, "created"

    text = rule_file.read_text(encoding="utf-8")

    # New format (markers present) -> replace the block
    if MDDB_MARKER_START in text and MDDB_MARKER_END in text:
        before, _, rest = text.partition(MDDB_MARKER_START)
        _, _, after = rest.partition(MDDB_MARKER_END)
        before = before.rstrip()
        after = after.lstrip()
        parts: list[str] = []
        if before:
            parts.append(before)
            parts.append("")
        parts.append(snippet.rstrip())
        if after:
            parts.append("")
            parts.append(after)
        new_text = "\n".join(parts)
        if not new_text.endswith("\n"):
            new_text += "\n"
        rule_file.write_text(new_text, encoding="utf-8")
        return rule_file, "updated"

    # New append
    if text.endswith("\n\n"):
        prefix = ""
    elif text.endswith("\n"):
        prefix = "\n"
    else:
        prefix = "\n\n"
    rule_file.write_text(text + prefix + snippet, encoding="utf-8")
    return rule_file, "appended"


def write_rules_snippet_multi(
    project_root: Path,
    tools: list[str],
    data_rel: str,
) -> list[tuple[str, Path, str]]:
    """Process multiple tools (or ``"all"``) at once.

    Returns:
        ``[(tool, rule_file, status), ...]``
    """

    targets = list(SUPPORTED_TOOLS) if any(t == "all" for t in tools) else tools
    out: list[tuple[str, Path, str]] = []
    for t in targets:
        rule_file, status = write_rules_snippet(project_root, t, data_rel)
        out.append((t, rule_file, status))
    return out


def _ensure_hook_command(
    hooks_root: dict[str, object], event: str, command: str
) -> bool:
    """If ``command`` is not in ``hooks_root[event]``, append it. Returns whether anything was added."""

    bucket_obj = hooks_root.setdefault(event, [])
    if not isinstance(bucket_obj, list):  # pragma: no cover
        raise RuntimeError(f"settings.json: 'hooks.{event}' must be JSON array")
    bucket: list[object] = bucket_obj

    for entry in bucket:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("hooks", [])
        if not isinstance(inner, list):
            continue
        for h in inner:
            if isinstance(h, dict) and h.get("command") == command:
                return False

    bucket.append({"hooks": [{"type": "command", "command": command}]})
    return True


def _command_in_settings(loaded: object, event: str, command: str) -> bool:
    if not isinstance(loaded, dict):
        return False
    hooks_root = loaded.get("hooks", {})
    if not isinstance(hooks_root, dict):
        return False
    bucket = hooks_root.get(event, [])
    if not isinstance(bucket, list):
        return False
    for entry in bucket:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("hooks", [])
        if not isinstance(inner, list):
            continue
        for h in inner:
            if isinstance(h, dict) and h.get("command") == command:
                return True
    return False


# ---------------------------------------------------------------------------
# Console matrix for no-option init
# ---------------------------------------------------------------------------


def render_tool_matrix(data_rel: str) -> str:
    """Tool matrix guide printed on the console for ``mddbai init`` with no options."""

    one_liner = f"Memory in `{data_rel}/`. See `{data_rel}/_AGENT_GUIDE.md`."
    lines = [
        "",
        "[mddbai] init done. To let the parent project's AI auto-detect this,",
        "       paste a one-liner into your tool's rule file from below:",
        "",
        f"  Claude Code  -> CLAUDE.md                  :  {one_liner}",
        f"  Cursor       -> .cursor/rules/mddbai.mdc     :  (auto-generated, includes frontmatter)",
        f"  Gemini CLI   -> GEMINI.md                  :  {one_liner}",
        f"  Codex CLI    -> AGENTS.md                  :  {one_liner}",
        "",
        "  Real enforcement (Claude Code only):  mddbai init <data> --with-claude-hook",
        "                                         (or pick y/N in interactive mode)",
        "  Auto-write (explicit user request):    mddbai init <data> --write-rules-snippet=<tool>",
        "  Toggle hooks later:                    mddbai hook enable / disable / status",
        "",
    ]
    return "\n".join(lines)

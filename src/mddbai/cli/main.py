from __future__ import annotations

"""``mddbai`` CLI entry point."""

import json
import sys
from pathlib import Path
from typing import Any

import typer

from mddbai.cli import _strict
from mddbai.core.config import MddbConfig
from mddbai.core.types import TableName
from mddbai.engine import Database

app = typer.Typer(help="MDDB — Markdown Database CLI", no_args_is_help=True)


def _open(data_dir: Path) -> Database:
    cfg = MddbConfig(data_dir=data_dir)
    return Database(data_dir, config=cfg)


@app.command()
def init(
    data_dir: Path = typer.Argument(
        Path(".mddbai"),
        help="Data directory (default: .mddbai — install-layout.md aligned standard)",
    ),
    with_claude_hook: bool = typer.Option(
        False,
        "--with-claude-hook",
        help="Auto-install Claude Code 4 hooks (SessionStart / PreToolUse / UserPromptSubmit / Stop) + skills (Claude Code only). Skip interactive prompt.",
    ),
    no_claude_hook: bool = typer.Option(
        False,
        "--no-claude-hook",
        help="Skip hook installation without showing the interactive prompt.",
    ),
    write_rules_snippet: str = typer.Option(
        "",
        "--write-rules-snippet",
        help="Write mddbai snippet to per-tool rule files. Values: claude|cursor|gemini|codex|all. "
        "Comma-separated allowed (e.g. claude,cursor).",
    ),
    project_root: Path | None = typer.Option(
        None,
        "--project-root",
        help="Target project root for hook / rule files. Default: parent of data_dir",
    ),
) -> None:
    """Initialize a new data directory. Creates ``<data_dir>/``.

    No-option call: in a TTY, asks interactively whether to install Claude Code
    hooks; otherwise prints a per-tool copy-paste matrix without *touching* user
    files. Toggle later with ``mddbai hook enable / disable / status``.

    ``_AGENT_GUIDE.md`` is not auto-generated. The folder itself is its own
    metadata. To embed identity (purpose / scale / axes / fallback) call
    ``mddbai palace init`` separately.
    """

    from mddbai.brain import agent_guide as _agent_guide  # noqa: PLC0415
    from mddbai.cli import integration as _intg  # noqa: PLC0415

    data_dir.mkdir(parents=True, exist_ok=True)
    with _open(data_dir):
        pass
    guide_path = _agent_guide.ensure(data_dir)
    typer.echo(f"initialized: {data_dir}")
    typer.echo(f"  guide: {guide_path}")

    root = (project_root or data_dir.resolve().parent).resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        data_rel = data_dir.resolve().relative_to(root).as_posix()
    except ValueError:
        data_rel = data_dir.as_posix()

    # Handle --write-rules-snippet=<tool>[,<tool>,...]
    requested_any_integration = bool(with_claude_hook or write_rules_snippet)

    if write_rules_snippet:
        tools = [t.strip() for t in write_rules_snippet.split(",") if t.strip()]
        try:
            results = _intg.write_rules_snippet_multi(root, tools, data_rel)
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from None
        for tool, rule_file, status in results:
            typer.echo(f"  {tool}: {rule_file} [{status}]")

    # Interactive prompt — whether to install Claude Code hooks.
    # Only ask when neither --with-claude-hook nor --no-claude-hook is given,
    # no other integration option was passed, and we're in a TTY.
    install_hook = with_claude_hook
    if not with_claude_hook and not no_claude_hook and not requested_any_integration:
        import sys as _sys  # noqa: PLC0415

        if _sys.stdin.isatty() and _sys.stdout.isatty():
            answer = typer.prompt(
                "\n[mddbai] Install Claude Code 4 hooks + skills?\n"
                "       (Embeds mddbai-native python hooks at SessionStart /\n"
                "        PreToolUse / UserPromptSubmit / Stop. Actually blocks\n"
                "        whole-file Grep / Read bypasses, and accumulates valuable\n"
                "        lines from user/AI utterances into a lexicon drawer.\n"
                "        Claude Code only — other AI tools get rule file guides only.\n"
                "        Disable later with `mddbai hook disable`.)\n"
                "  Install? [y/N]",
                default="N",
                show_default=False,
            )
            if answer.strip().lower() in {"y", "yes"}:
                install_hook = True

    if install_hook:
        full = _intg.install_claude_full(root)
        typer.echo(
            f"  python hooks: {len(full['hooks'])} files -> "
            f".claude/hooks/mddbai_*.py [installed]"
        )
        typer.echo(
            f"  skills: {len(full['skills'])} files -> "
            f".claude/skills/mddbai-*/SKILL.md [installed]"
        )
        typer.echo(f"  settings: {full['settings']} [{full['settings_status']}]")

    # Show console matrix only when *no* integration option was given and no hook was installed
    if not requested_any_integration and not install_hook:
        typer.echo(_intg.render_tool_matrix(data_rel))


# Stage W: removed put / get / delete / scan / range commands (record model deprecated)

# ---- Stage Z.6 — drawer model CLI -----------------------------------------
#
# R4 library vocabulary: place / take / browse / whole-view. The AI uses this
# as the primary retrieval path. Small drawers whole, large drawers by section.


@app.command()
def take(
    data_dir: Path,
    table: str = typer.Argument(..., help="Table name"),
    drawer: str = typer.Argument(..., help="Drawer (e.g. facts/medicine)"),
    section: str = typer.Argument(
        "",
        help="Section ID (H2 heading text). Empty returns the whole drawer.",
    ),
    body_only: bool = typer.Option(
        False, "--body-only", help="Strip the H2 heading line and print body only (R4 aligned)"
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Strict retrieval mode: forbids whole-drawer take with no section, "
        f"and sections over {_strict.DEFAULT_LARGE_SECTION_BYTES}B require "
        "--allow-large-dump. Also enabled by env MDDBAI_STRICT_RETRIEVAL=1 or "
        "MddbConfig.navigation_strict.",
    ),
    allow_large_dump: bool = typer.Option(
        False,
        "--allow-large-dump",
        help="Explicit consent to bypass large section / drawer dump in strict mode. "
        "Emits a 'strict retrieval bypass' warning on stderr.",
    ),
) -> None:
    """Take one section of a drawer, or the whole drawer.

    Examples::

        mddbai take .data plans 02-master-plan
        mddbai take .data plans 02-master-plan "9. decision log"
        mddbai take .data plans 02-master-plan "9. decision log" --body-only
        mddbai take .data plans 02-master-plan --strict   # ERROR: section required
    """

    with _open(data_dir) as db:
        config_strict = bool(getattr(db.config, "navigation_strict", False))
        is_strict = _strict.strict_active(flag=strict, config_strict=config_strict)

        if not section:
            if is_strict and not allow_large_dump:
                typer.echo(
                    "error: strict retrieval forbids drawer-wide take. "
                    "Specify a section id, or pass --allow-large-dump to override.",
                    err=True,
                )
                raise typer.Exit(code=2)
            if is_strict and allow_large_dump:
                _strict.warn_bypass(
                    f"drawer-wide take {table}/{drawer}",
                )
            text = db.take_drawer(table, drawer)
        else:
            text = db.take_section(table, drawer, section, body_only=body_only)
            if text is not None and is_strict:
                size = len(text.encode("utf-8"))
                limit = _strict.large_section_threshold()
                if size > limit and not allow_large_dump:
                    typer.echo(
                        f"error: strict retrieval forbids large section dump "
                        f"({size}B > {limit}B). Pass --allow-large-dump to override, "
                        "or split the section (mddbai split-drawer or finer-grained "
                        "ingest).",
                        err=True,
                    )
                    raise typer.Exit(code=2)
                if size > limit and allow_large_dump:
                    _strict.warn_bypass(
                        f"large section take {table}/{drawer}#{section}",
                        size_bytes=size,
                    )
    if text is None:
        raise typer.Exit(code=1)
    typer.echo(text)


@app.command("ingest-document")
def ingest_document_cmd(
    data_dir: Path,
    table: str = typer.Argument(..., help="Target table"),
    drawer: str = typer.Argument(..., help="Target drawer (sections are written into this)"),
    input_file: Path = typer.Argument(..., help="Source .md / .txt file"),
    heading_regex: str = typer.Option(
        r"^##\s+(.+)$",
        "--split-by-heading",
        help="Regex marking section boundaries. Default is markdown H2. "
        "For numbered documents, callers pass their own pattern "
        "(e.g. '^##\\s+(\\d+:\\d+)'). No domain-specific wording.",
    ),
    id_capture_group: int = typer.Option(
        1,
        "--id-group",
        help="Capture group index in the heading regex (used as id). 0 = whole match.",
    ),
    min_body_chars: int = typer.Option(
        0,
        "--min-body-chars",
        help="Bodies shorter than this are not written as sections (filters small noise).",
    ),
    fsync: bool = typer.Option(True, "--fsync/--no-fsync"),
    no_auto_cue: bool = typer.Option(
        False,
        "--no-auto-cue",
        help="Disable automatic structural cue preservation. Default is automatic.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Only split, don't write. Print section ids only."
    ),
) -> None:
    """Split a source .md with a generic splitter and write it as sections.

    Strict-retrieval-aligned ingest. The path that writes the entire body as
    a single ``body`` is *blocked by default*; instead the document is split
    using a heading pattern chosen by the AI/user, so that navigate can
    pinpoint the exact section.

    Examples::

        # split by markdown H2 (default)
        mddbai ingest-document .mddbai notes my-essay essay.md

        # finer granularity (H3)
        mddbai ingest-document .mddbai notes spec spec.md \\
            --split-by-heading '^###\\s+(.+)$'

        # numbered pattern (e.g. 1.1, 2.3.4)
        mddbai ingest-document .mddbai docs rules rules.md \\
            --split-by-heading '^##\\s+(\\d+(?:\\.\\d+)*)\\s'
    """

    # T.6: log bypass route + hint (does not refuse the action)
    typer.echo(
        "[hint] The front-door 'mddbai write' improves next-recall. Proceeding anyway.",
        err=True,
    )
    from mddbai.brain import tutorial as _tut  # noqa: PLC0415
    _tut.record_call(data_dir, cmd="ingest-document", via_door=False)

    from mddbai.codec.splitter import SplitterError, split_by_heading  # noqa: PLC0415

    if not input_file.exists() or not input_file.is_file():
        typer.echo(f"error: input file not found: {input_file}", err=True)
        raise typer.Exit(code=2)

    text = input_file.read_text(encoding="utf-8")

    try:
        sections = split_by_heading(
            text,
            heading_regex=heading_regex,
            id_capture_group=id_capture_group,
            min_body_chars=min_body_chars,
        )
    except SplitterError as exc:
        typer.echo(f"error: splitter: {exc}", err=True)
        raise typer.Exit(code=2) from None

    if not sections:
        typer.echo(
            "error: heading regex matched 0 sections. "
            "Check --split-by-heading or use a different pattern. "
            "Refusing to dump entire body as a single section "
            "(strict retrieval discipline).",
            err=True,
        )
        raise typer.Exit(code=2)

    if dry_run:
        for s in sections:
            typer.echo(f"{s.index:04d}\t{s.id}\t{len(s.body)}B\t{s.heading[:60]}")
        typer.echo(f"# total: {len(sections)} sections", err=True)
        return

    from mddbai.codec.section_meta import derive_structural_cue  # noqa: PLC0415

    with _open(data_dir) as db:
        for s in sections:
            db.put_section(table, drawer, s.id, s.body, fsync=fsync)
            if not no_auto_cue:
                existing = db.get_section_meta(table, drawer, s.id) or {}
                if not existing.get("cue"):
                    derived = derive_structural_cue(
                        section_id=s.id,
                        drawer_rel=drawer,
                        heading=s.heading,
                        body_preview=s.body[:80],
                    )
                    if derived:
                        db.put_section_meta(
                            table, drawer, s.id, cue=derived, merge=True
                        )

    typer.echo(f"- ingested {len(sections)} sections into {table}/{drawer}")


@app.command("put-section")
def put_section_cmd(
    data_dir: Path,
    table: str,
    drawer: str,
    section: str,
    body_pos: str = typer.Argument(
        "",
        help="(positional) Section body. With a `-` prefix typer mistakes it "
        "for an option; use --body / --body-stdin / --body-file in that case.",
    ),
    body: str = typer.Option(
        "",
        "--body",
        help="Section body as an option. Avoids the positional `-` prefix pitfall.",
    ),
    body_stdin: bool = typer.Option(
        False, "--body-stdin", help="Read body from stdin in full."
    ),
    body_file: Path | None = typer.Option(
        None, "--body-file", help="Read body from a file."
    ),
    fsync: bool = typer.Option(True, "--fsync/--no-fsync"),
    cue: list[str] = typer.Option(
        [],
        "--cue",
        help="Recall cue (repeatable). Trace written by the AI. If omitted, "
        "*structural* cues are auto-preserved from section_id + drawer path tokens "
        "(no semantic extraction).",
    ),
    importance: float | None = typer.Option(
        None,
        "--importance",
        min=0.0,
        max=1.0,
        help="Importance 0.0~1.0 (decided by the AI).",
    ),
    related: list[str] = typer.Option(
        [],
        "--related",
        help="Adjacent drawer/section paths (repeatable). e.g. notes/diary/2026-05-01",
    ),
    memory_zone: str = typer.Option(
        "",
        "--memory-zone",
        help="hot|warm|cold|archive (decided by the AI).",
    ),
    no_auto_cue: bool = typer.Option(
        False,
        "--no-auto-cue",
        help="Disable automatic structural cue preservation. Default is automatic (write-time signal).",
    ),
    entity: list[str] = typer.Option(
        [],
        "--entity",
        help="Noun-form keywords (repeatable). Multi-representation cue.",
    ),
    date: str = typer.Option(
        "", "--date", help="ISO 8601 date (YYYY-MM-DD). Event timestamp."
    ),
    source: str = typer.Option(
        "", "--source", help="ai|user|cite|tool|import — origin classification."
    ),
    confidence: float | None = typer.Option(
        None,
        "--confidence",
        min=0.0,
        max=1.0,
        help="0.0~1.0 — AI-assigned confidence.",
    ),
    state: str = typer.Option(
        "",
        "--state",
        help="active|superseded|deprecated — revision protocol.",
    ),
    current_revision: str = typer.Option(
        "",
        "--current-revision",
        help="Latest revision id (e.g. r3). None = single revision.",
    ),
    supersedes: list[str] = typer.Option(
        [],
        "--supersedes",
        help="Old revision ids that this section replaces (repeatable).",
    ),
    recall_check: bool = typer.Option(
        False,
        "--recall-check",
        help="Run a navigate self-test right after writing. If the section is "
        "not retrievable from its own cue, emits a one-line hint on stderr.",
    ),
    lang: str = typer.Option(
        "auto",
        "--lang",
        help=(
            "Language inference hint, 1 char. 'auto' (default) = auto-detect "
            "from body + cue. Explicit: 'ko' / 'en' etc. '' = do not store."
        ),
    ),
) -> None:
    """Place body content into a section of a drawer (= put). Replaces an existing section.

    K.2 (2026-05-05) — 4 body input paths (positional / --body / --body-stdin /
    --body-file). Positional is for compatibility. Bodies starting with `-`
    (e.g. action item `- [ ] ...`) are safer in option form.

    Specify exactly one. Two or more is an error. All empty is an error.

    Output is a single .md-friendly line (key=value). Aligned with the principle
    (markdown surface).
    """

    # T.6: log bypass route + hint (does not refuse the action)
    typer.echo(
        "[hint] The front-door 'mddbai write' improves next-recall. Proceeding anyway.",
        err=True,
    )
    from mddbai.brain import tutorial as _tut  # noqa: PLC0415
    _tut.record_call(data_dir, cmd="put-section", via_door=False)

    body_sources = sum(
        [
            1 if body_pos else 0,
            1 if body else 0,
            1 if body_stdin else 0,
            1 if body_file is not None else 0,
        ]
    )
    if body_sources == 0:
        typer.echo(
            "error: body not provided. one of positional / --body / --body-stdin / --body-file is required.",
            err=True,
        )
        raise typer.Exit(code=2)
    if body_sources > 1:
        typer.echo(
            "error: more than one body input path provided. choose only one of positional / --body / --body-stdin / --body-file.",
            err=True,
        )
        raise typer.Exit(code=2)

    if body_stdin:
        resolved_body = sys.stdin.read()
    elif body_file is not None:
        resolved_body = body_file.read_text(encoding="utf-8")
    elif body:
        resolved_body = body
    else:
        resolved_body = body_pos

    with _open(data_dir) as db:
        stat = db.put_section(table, drawer, section, resolved_body, fsync=fsync)

        # Write explicit cue / importance / related / memory_zone / entity /
        # date / source / confidence / state / current_revision / supersedes.
        # merge=True preserves None positions from prior calls.
        zone_arg: str | None = memory_zone or None
        date_arg: str | None = date or None
        source_arg: str | None = source or None
        state_arg: str | None = state or None
        cur_rev_arg: str | None = current_revision or None
        explicit_cue = list(cue) if cue else None
        explicit_related = list(related) if related else None
        explicit_entity = list(entity) if entity else None
        explicit_supersedes = list(supersedes) if supersedes else None

        # Language inference hint
        from mddbai.codec.section_meta import detect_lang as _detect_lang  # noqa: PLC0415

        lang_norm = lang.strip().lower()
        lang_arg: str | None
        if lang_norm == "":
            lang_arg = None
        elif lang_norm == "auto":
            lang_arg = _detect_lang(
                resolved_body or "",
                *(explicit_cue or []),
                *(explicit_entity or []),
                section or "",
            )
        else:
            lang_arg = lang_norm

        any_meta = (
            explicit_cue is not None
            or importance is not None
            or explicit_related is not None
            or zone_arg is not None
            or explicit_entity is not None
            or date_arg is not None
            or source_arg is not None
            or confidence is not None
            or state_arg is not None
            or cur_rev_arg is not None
            or explicit_supersedes is not None
            or lang_arg is not None
        )
        if any_meta:
            db.put_section_meta(
                table,
                drawer,
                section,
                cue=explicit_cue,
                importance=importance,
                related=explicit_related,
                memory_zone=zone_arg,
                entity=explicit_entity,
                date=date_arg,
                source=source_arg,
                confidence=confidence,
                state=state_arg,
                current_revision=cur_rev_arg,
                supersedes=explicit_supersedes,
                lang=lang_arg,
                merge=True,
            )

        # When no explicit cue and auto is enabled: auto-preserve structural cue.
        # If cues already exist, preserve them (merge=True).
        if not no_auto_cue and not explicit_cue:
            existing = db.get_section_meta(table, drawer, section) or {}
            if not existing.get("cue"):
                from mddbai.codec.section_meta import (  # noqa: PLC0415
                    derive_structural_cue,
                )

                preview = resolved_body[:80] if resolved_body else ""
                derived = derive_structural_cue(
                    section_id=section,
                    drawer_rel=drawer,
                    heading=section,
                    body_preview=preview,
                )
                if derived:
                    db.put_section_meta(
                        table,
                        drawer,
                        section,
                        cue=derived,
                        merge=True,
                    )

        # Future Recall Check (4.2 protocol §6) — navigate self-test right after writing.
        # Does not auto-augment cues (D2 aligned), reports only to the AI.
        # 2026-05-07 fix: flush dirty buffer first, then run navigate using the
        # same section-level hit criterion as recall --strict (frontmatter /
        # sections_meta must be on disk for navigate to see them — fixes prior false negatives).
        if recall_check:
            try:
                db.flush()
                self_cue = " ".join(
                    (explicit_cue or [])
                    + (explicit_entity or [])
                    + [section]
                )[:100]
                if self_cue.strip():
                    nav = db.navigate(
                        self_cue,
                        max_routes=5,
                        fallback_disabled=True,
                    )
                    section_routes = [
                        r
                        for r in (nav.get("routes") or [])
                        if r.get("table") and r.get("drawer") and r.get("section")
                    ]
                    hit = any(
                        r["table"] == table
                        and r["drawer"] == drawer
                        and r["section"] == section
                        for r in section_routes
                    )
                    if hit:
                        typer.echo(
                            f"# recall-check: ok ({table}/{drawer}#{section} "
                            "reachable from self cue)",
                            err=True,
                        )
                    else:
                        cands = [
                            f"{r['table']}/{r['drawer']}#{r['section']}"
                            for r in section_routes[:3]
                        ]
                        typer.echo(
                            "# recall-check: section not yet retrievable by self "
                            f"cue '{self_cue.strip()}'.",
                            err=True,
                        )
                        if cands:
                            typer.echo(
                                "# recall-check: closest section-level "
                                f"candidates: {', '.join(cands)}",
                                err=True,
                            )
                        typer.echo(
                            "# recall-check: add more --cue / --entity / "
                            "--related tokens (Future Recall protocol §6). "
                            "No body dump.",
                            err=True,
                        )
            except Exception as exc:  # noqa: BLE001
                typer.echo(
                    f"# recall-check: skipped (navigate failed: {exc})", err=True
                )

    pct = float(stat.get("size_pct_of_threshold", 0.0))
    size = int(stat.get("drawer_size_bytes", 0))
    sections = int(stat.get("section_count", 0))
    threshold = int(stat.get("split_threshold_bytes", 0))
    if stat.get("split_recommended"):
        split_state = "now"
    elif pct >= 0.8:
        split_state = "soon"
    else:
        split_state = "ok"
    typer.echo(
        f"- put_section drawer={size}B/{threshold}B pct={pct:.3f} "
        f"sections={sections} split={split_state}"
    )


@app.command("list-sections")
def list_sections_cmd(
    data_dir: Path,
    table: str,
    drawer: str,
) -> None:
    """List section IDs inside a drawer (= browse)."""

    with _open(data_dir) as db:
        for sid in db.list_sections(table, drawer):
            typer.echo(sid)


@app.command("cues")
def cues_cmd(
    data_dir: Path,
    table: str = typer.Argument(
        "",
        help="Table name. If omitted, dumps all tables (cold AI's first tool).",
    ),
    depth: int = typer.Option(
        2,
        "--depth",
        help="Folders deeper than N levels are *not read* (prevents IO blowup at cold start). "
             "default 2 = shelf -> sub-folder -> book. 0 = all.",
    ),
    prefix: str = typer.Option(
        "",
        "--prefix",
        help="Show only items inside this sub-folder. e.g. --prefix dogma/",
    ),
) -> None:
    """Dump cue traces (frontmatter + section IDs) per drawer.

    Aligned with identity.md I.3 (zero search) + I.4 (traces). Shows only the
    cues the AI placed — no matching / embedding / grep. The AI receives this
    in one shot and reasons its way to the exact section.

    *cold AI assumption* — called with empty AI context. Initial entry points:

        mddbai cues .mddbai              # dump all shelves (auto ls of tables)
        mddbai cues .mddbai decisions    # single shelf
        mddbai cues .mddbai decisions --prefix dogma/   # sub-folder only

    Output is JSON. No body content (lightweight dump). After reasoning, the
    AI calls ``mddbai take`` to fetch only the exact section body (V3 aligned).
    """

    with _open(data_dir) as db:
        if not table:
            # cold AI — dump every shelf. ls table folders from disk.
            tables: list[str] = []
            if data_dir.exists():
                from mddbai.engine import _is_skippable_dir_name  # noqa: PLC0415

                for p in data_dir.iterdir():
                    if p.is_dir() and not _is_skippable_dir_name(p.name):
                        tables.append(p.name)
            tables.sort()
            all_cues: dict[str, list[dict[str, object]]] = {}
            for t in tables:
                try:
                    all_cues[t] = db.list_cues(t, depth=depth, prefix=prefix or None)
                except Exception as e:  # noqa: BLE001
                    all_cues[t] = [{"error": str(e)}]
            typer.echo(json.dumps(all_cues, ensure_ascii=False, indent=2))
        else:
            cues = db.list_cues(table, depth=depth, prefix=prefix or None)
            typer.echo(json.dumps(cues, ensure_ascii=False, indent=2))


@app.command("list-drawers")
def list_drawers_cmd(
    data_dir: Path,
    table: str,
    depth: int = typer.Option(
        0,
        "--depth",
        help="0 = flat all (but auto-clamped to depth=1 when over flat_dump_threshold). "
             "N>0 = up to N levels. Prevents context blowup with tens of thousands of drawers "
             "(aligned with identity.md I.2).",
    ),
    prefix: str = typer.Option(
        "",
        "--prefix",
        help="Show only items inside this sub-folder. e.g. --prefix dogma/",
    ),
    flat: bool = typer.Option(
        False,
        "--flat",
        help="Bypass auto depth clamp. Force flat dump even over flat_dump_threshold.",
    ),
) -> None:
    """List drawers inside a table.

    Examples (tens of thousands of drawers)::

        mddbai list-drawers .mddbai decisions --depth 1
        # -> ['dogma/', 'architecture/', 'identity.md']
        # Folders (`/` suffix) have books deeper inside. Drill in:

        mddbai list-drawers .mddbai decisions --prefix dogma/ --depth 1
        # -> ['core', 'interpretation']
    """

    with _open(data_dir) as db:
        for d in db.list_drawers(
            table, depth=depth, prefix=prefix or None, force_flat=flat
        ):
            typer.echo(d)


@app.command("summarize-drawers")
def summarize_drawers_cmd(
    data_dir: Path,
    table: str,
    prefix: str = typer.Option("", "--prefix", help="Summarize only inside this sub-folder"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Pre-flat-dump navigation summary (total drawer count + per-folder counts).

    Lets a cold AI check blowup risk. If would_explode=true, depth>=1 is recommended.
    """

    with _open(data_dir) as db:
        info = db.summarize_drawers(table, prefix=prefix or None)
    if json_output:
        typer.echo(json.dumps(info, ensure_ascii=False, indent=2))
        return
    typer.echo(f"table: {info['table']}")
    typer.echo(f"total_drawers: {info['total_drawers']}")
    typer.echo(f"threshold: {info['threshold']}")
    typer.echo(f"would_explode: {info['would_explode']}")
    typer.echo(f"recommended_depth: {info['recommended_depth']}")
    typer.echo(f"root_drawers: {info['root_drawers']}")
    folders = info["top_folders"]
    if isinstance(folders, list) and folders:
        typer.echo("top_folders:")
        for entry in folders:
            if isinstance(entry, dict):
                typer.echo(f"  {entry.get('folder')}: {entry.get('count')}")


@app.command("delete-section")
def delete_section_cmd(
    data_dir: Path,
    table: str,
    drawer: str,
    section: str,
) -> None:
    """Remove one section of a drawer."""

    # T.6: log bypass route + hint (does not refuse the action)
    typer.echo(
        "[hint] The front-door 'mddbai write' improves next-recall. Proceeding anyway.",
        err=True,
    )
    from mddbai.brain import tutorial as _tut  # noqa: PLC0415
    _tut.record_call(data_dir, cmd="delete-section", via_door=False)

    with _open(data_dir) as db:
        ok = db.delete_section(table, drawer, section)
    if not ok:
        raise typer.Exit(code=1)
    typer.echo("ok")


@app.command("rename-section")
def rename_section_cmd(
    data_dir: Path,
    table: str,
    drawer: str,
    old_section: str,
    new_section: str,
) -> None:
    """Rename a section ID (body preserved). One-line replace from temp slug to meaningful slug.

    AI-friendly entry point — aligned with identity.md I.4. Re-writes body under
    the new ID and removes the old ID. Serialized inside the same drawer's FileLock.

    Example::

        mddbai rename-section .mddbai sessions 2026-05 s925a2eef mddbai-identity-fix

    Failure cases (exit 1):
    - Old section missing
    - New ID already exists (caller resolves the conflict explicitly)
    """

    # T.6: log bypass route + hint (does not refuse the action)
    typer.echo(
        "[hint] The front-door 'mddbai write' improves next-recall. Proceeding anyway.",
        err=True,
    )
    from mddbai.brain import tutorial as _tut  # noqa: PLC0415
    _tut.record_call(data_dir, cmd="rename-section", via_door=False)

    with _open(data_dir) as db:
        ok = db.rename_section(table, drawer, old_section, new_section)
    if not ok:
        typer.echo(
            f"rename failed: old='{old_section}' new='{new_section}' "
            f"(missing or conflict)",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"renamed: {old_section} -> {new_section}")


@app.command("rename-drawer")
def rename_drawer_cmd(
    data_dir: Path,
    table: str,
    old_drawer: str,
    new_drawer: str,
) -> None:
    """Rename a drawer (body preserved as a whole). Move only within the same table.

    AI-friendly entry point — aligned with identity.md I.4. Atomic whole-file
    move on disk. Auto-updates the `_drawer_id` frontmatter. Invalidates cache.

    Example::

        mddbai rename-drawer .mddbai sessions 2026/05/05/s925a2eef sessions/2026-05-mddbai-identity

    Failure cases (exit 1):
    - Old drawer missing
    - New drawer already exists (conflict)
    """

    # T.6: log bypass route + hint (does not refuse the action)
    typer.echo(
        "[hint] The front-door 'mddbai write' improves next-recall. Proceeding anyway.",
        err=True,
    )
    from mddbai.brain import tutorial as _tut  # noqa: PLC0415
    _tut.record_call(data_dir, cmd="rename-drawer", via_door=False)

    with _open(data_dir) as db:
        ok = db.rename_drawer(table, old_drawer, new_drawer)
    if not ok:
        typer.echo(
            f"rename failed: old='{old_drawer}' new='{new_drawer}' "
            f"(missing or conflict)",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"renamed: {old_drawer} -> {new_drawer}")


@app.command("split-drawer")
def split_drawer_cmd(
    data_dir: Path,
    table: str,
    drawer: str,
    by: str = typer.Option(
        "time",
        "--by",
        help="Split strategy. 'time' = bisect chronologically / 'manual' = use --plan JSON",
    ),
    plan: Path | None = typer.Option(
        None,
        "--plan",
        help="Manual split plan JSON file ({\"new_drawer\": [\"section_id\", ...]}).",
    ),
    fsync: bool = typer.Option(True, "--fsync/--no-fsync"),
) -> None:
    """Split a single drawer into N drawers (Stage Z.7).

    Examples::

        mddbai split-drawer .data plans 02-master-plan --by time
        mddbai split-drawer .data notes diary/2026-05-02 --by time
        mddbai split-drawer .data notes facts/medicine \\
            --by manual --plan ./split-plan.json

    plan JSON format (manual)::

        {
          "facts/painkillers": ["tylenol", "ibuprofen"],
          "facts/vitamins": ["vitamin-d"]
        }
    """

    # T.6: log bypass route + hint (does not refuse the action)
    typer.echo(
        "[hint] The front-door 'mddbai write' improves next-recall. Proceeding anyway.",
        err=True,
    )
    from mddbai.brain import tutorial as _tut  # noqa: PLC0415
    _tut.record_call(data_dir, cmd="split-drawer", via_door=False)

    new_drawers: dict[str, list[str]] | None = None
    if by == "manual":
        if plan is None:
            raise typer.BadParameter("--plan required when --by manual")
        try:
            data = json.loads(plan.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise typer.BadParameter(f"failed to read plan {plan}: {exc}") from exc
        if not isinstance(data, dict):
            raise typer.BadParameter("plan JSON must be an object")
        new_drawers = {str(k): [str(s) for s in v] for k, v in data.items()}

    with _open(data_dir) as db:
        try:
            written = db.split_drawer(
                table, drawer, by=by, new_drawers=new_drawers, fsync=fsync
            )
        except (ValueError, FileNotFoundError) as exc:
            typer.echo(f"split failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    for d in written:
        typer.echo(d)


@app.command("related")
def related_cmd(
    data_dir: Path,
    table: str = typer.Argument(..., help="Table name"),
    drawer: str = typer.Argument(..., help="drawer path"),
    hops: int = typer.Option(
        1,
        "--hops",
        help="Traversal depth (0=direct links only, 1=1 hop, 2=2 hop, ...)",
    ),
    max_drawers: int = typer.Option(
        50, "--max-drawers", help="Max drawers in result (prevents blowup)"
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Traverse the drawer's related graph — collect adjacent drawers.

    Examples::

        mddbai related .mddbai decisions identity --hops 1
        mddbai related .mddbai decisions identity --hops 2 --json
    """

    with _open(data_dir) as db:
        if hops <= 0:
            refs = db.collect_related(table, drawer)
            if json_output:
                typer.echo(json.dumps(refs, ensure_ascii=False, indent=2))
                return
            if not refs:
                typer.echo("(no related links)")
                return
            for r in refs:
                marker = "" if r.get("exists") else " [BROKEN]"
                sid = r.get("section_id")
                origin = f"#{sid}" if sid else ""
                typer.echo(f"{r['source']}{origin} -> {r['target']}{marker}")
            return

        hops_out = db.traverse_related(
            table, drawer, max_hops=hops, max_drawers=max_drawers
        )
        if json_output:
            typer.echo(json.dumps(hops_out, ensure_ascii=False, indent=2))
            return
        for h in hops_out:
            typer.echo(f"d={h['distance']}  {h['drawer']}")


@app.command("related-broken")
def related_broken_cmd(
    data_dir: Path,
    table: str = typer.Option("", "--table", help="Inspect a single table only"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Find related targets across all drawers that do not exist on disk (broken links)."""

    with _open(data_dir) as db:
        broken = db.find_broken_related(table or None)
    if json_output:
        typer.echo(json.dumps(broken, ensure_ascii=False, indent=2))
        return
    if not broken:
        typer.echo("(no broken related links)")
        return
    for b in broken:
        sid = b.get("section_id")
        origin = f"#{sid}" if sid else ""
        typer.echo(f"{b['source']}{origin} -> {b['target']}")


@app.command("navigate")
def navigate_cmd(
    data_dir: Path,
    cue: str = typer.Argument(..., help="Natural-language cue (e.g. 'why v1 was locked')"),
    max_routes: int = typer.Option(
        5, "--max-routes", help="Max routes in output (default 5)"
    ),
    max_tables: int = typer.Option(
        5, "--max-tables", help="Internal cap on table candidates"
    ),
    max_drawers: int = typer.Option(
        20, "--max-drawers", help="Internal cap on drawer candidates"
    ),
    max_sections: int = typer.Option(
        50, "--max-sections", help="Internal cap on section candidates"
    ),
    include_reason: bool = typer.Option(
        True,
        "--include-reason/--no-reason",
        help="Print reason / signals for each route (default on)",
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Natural-language cue -> route candidate hints (no search engine, navigation assist).

    Does not read drawer bodies; performs lightweight matching only against
    cue traces already on disk (palace / table _summary.md / drawer names /
    section_id / sections_meta.cue / registry alias). Final semantic judgement
    is up to the AI.

    Examples::

        mddbai navigate .mddbai "why v1 was locked"
        mddbai navigate .mddbai "drawer model decision" --json
    """

    with _open(data_dir) as db:
        result = db.navigate(
            cue,
            max_routes=max_routes,
            max_tables=max_tables,
            max_drawers=max_drawers,
            max_sections=max_sections,
        )

    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return

    routes = result.get("routes") or []
    warnings = result.get("warnings") or []

    if not routes:
        typer.echo("No confident route found.")
        typer.echo("")
        typer.echo("Suggested next steps:")
        typer.echo("1. inspect table summaries (cat <data>/<table>/_summary.md)")
        typer.echo("2. narrow the cue (add one more cue word)")
        typer.echo("3. run list-drawers on likely table")
        for w in warnings:
            typer.echo(f"  warning: {w}", err=True)
        return

    typer.echo("Possible routes:")
    typer.echo("")
    for i, r in enumerate(routes, start=1):
        typer.echo(f"{i}.")
        typer.echo(str(r.get("table", "")))
        if r.get("drawer"):
            typer.echo(f"-> {r['drawer']}")
        if r.get("section"):
            typer.echo(f"-> {r['section']}")
        if include_reason:
            reason = r.get("reason") or "no specific signal"
            typer.echo("")
            typer.echo("reason:")
            typer.echo(reason)
            signals = r.get("signals") or []
            if signals:
                typer.echo(f"(signals: {', '.join(signals)})")
        typer.echo("")

    for w in warnings:
        typer.echo(f"warning: {w}", err=True)


@app.command("map")
def map_cmd(
    data_dir: Path,
    cue: str = typer.Argument(..., help="One-line natural-language cue. Drawer body 0 bytes; per-table _summary.md preview up to 4KB once."),
    max_routes: int = typer.Option(5, "--max-routes"),
    max_drawers: int = typer.Option(20, "--max-drawers"),
    max_sections: int = typer.Option(50, "--max-sections"),
    json_output: bool = typer.Option(False, "--json", help="JSON output."),
) -> None:
    """Parallel Navigation — routes + cues + summary preview + related edges in one dump.

    Step 3 of the multi-stage read protocol (4.1). Drawer bodies are *0 bytes*
    — section bodies are not read. Per-table ``_summary.md`` is previewed
    *up to 4KB once* (for route hinting). The cold AI receives this *map* in
    one call and decides the exact section for step 4 (Evidence Reading).

    No search command. No inference. No semantic decision. *Space dump only*.

    Examples::

        mddbai map .mddbai "why v1 was locked"
        mddbai map .mddbai "drawer model decision" --json
    """

    with _open(data_dir) as db:
        # routes (navigate, fallback allowed — exposes more candidates than strict)
        nav = db.navigate(
            cue,
            max_routes=max_routes,
            max_drawers=max_drawers,
            max_sections=max_sections,
            fallback_disabled=False,
        )
        routes = list(nav.get("routes") or [])

        # Collect candidate drawers — unique (table, drawer) from section/drawer-level routes
        seen_drawers: set[tuple[str, str]] = set()
        for r in routes:
            t = r.get("table")
            d = r.get("drawer")
            if t and d:
                seen_drawers.add((str(t), str(d)))

        # For each candidate drawer: section_id + related metadata (zero body)
        drawer_views: list[dict[str, Any]] = []
        for t, d in sorted(seen_drawers):
            try:
                section_ids = db.list_sections(t, d)
            except Exception:  # noqa: BLE001
                section_ids = []
            related_edges: list[str] = []
            try:
                from mddbai.codec.section_meta import (  # noqa: PLC0415
                    parse_sections_meta,
                )

                path = db._drawer_path(t, d)  # type: ignore[attr-defined]
                full = db.drawer_engine.cache.get_meta(path) or {}
                sm_map = parse_sections_meta(dict(full))
                for sid, sm in sm_map.items():
                    for rel in sm.related or []:
                        related_edges.append(f"{sid} -> {rel}")
            except Exception:  # noqa: BLE001
                pass
            drawer_views.append(
                {
                    "table": t,
                    "drawer": d,
                    "sections": list(section_ids),
                    "related_edges": related_edges[:10],
                }
            )

        # palace summary preview (already used by navigate with 4KB body cap — display only here)
        from mddbai.brain.palace_root import has_palace_root, read_palace_root  # noqa: PLC0415

        palace: dict[str, Any] | None = None
        if has_palace_root(Path(db.config.data_dir)):
            try:
                cfg = read_palace_root(Path(db.config.data_dir))
            except Exception:  # noqa: BLE001
                cfg = None
            if cfg is not None:
                palace = {
                    "purpose": cfg.purpose,
                    "scale": cfg.scale,
                    "axes": cfg.axes,
                    "fallback": cfg.fallback,
                }

        out: dict[str, Any] = {
            "cue": cue,
            "palace": palace,
            "routes": routes,
            "drawers": drawer_views,
            "warnings": list(nav.get("warnings") or []),
        }

    if json_output:
        typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
        return

    if palace and palace.get("purpose"):
        typer.echo(f"# palace.purpose: {palace['purpose'][:120]}")
    if not routes:
        typer.echo("# routes: (none — try a different cue or run `mddbai cues`)")
    else:
        typer.echo(f"# routes: {len(routes)}")
        for i, r in enumerate(routes, 1):
            tag = f"{r.get('table', '')}/{r.get('drawer') or '*'}"
            if r.get("section"):
                tag += f"#{r['section']}"
            typer.echo(f"  {i}. {tag}    [{','.join(r.get('signals') or [])}]")
    for dv in drawer_views:
        typer.echo(f"# drawer: {dv['table']}/{dv['drawer']} ({len(dv['sections'])} sections)")
        if dv["sections"]:
            typer.echo(f"  sections: {', '.join(dv['sections'][:8])}")
        if dv["related_edges"]:
            typer.echo(f"  related: {', '.join(dv['related_edges'][:5])}")
    for w in out["warnings"]:
        typer.echo(f"warning: {w}", err=True)


@app.command("link")
def link_cmd(
    data_dir: Path,
    a: str = typer.Argument(..., help="A: <table>/<drawer>#<section>"),
    b: str = typer.Argument(..., help="B: <table>/<drawer>#<section>"),
    bidir: bool = typer.Option(
        True,
        "--bidir/--unidir",
        help="Bidirectional (default). --unidir means A -> B only.",
    ),
    kind: str | None = typer.Option(
        None,
        "--kind",
        help=(
            "Typed relation kind (2026-05-09). When set, writes to the "
            "``relations`` field instead of plain ``related``. One of: "
            "refines, supersedes, contradicts, implies, depends-on, "
            "derived-from. AI decides the kind, DB only validates."
        ),
    ),
) -> None:
    """Relationship Linking — write a related edge between A and B (meta frontmatter).

    Step 5 of the multi-stage write protocol (4.2). No separate knowledge-graph
    search engine — only updates ``sections_meta[<sid>].related`` (untyped) or
    ``sections_meta[<sid>].relations`` (typed, when --kind is set) in markdown
    frontmatter.

    Examples::

        mddbai link .mddbai notes/diary#monday notes/diary#tuesday
        mddbai link .mddbai decisions/v1#lock decisions/dogma#d1 --unidir
        mddbai link .mddbai decisions/v2#lock decisions/v1#lock --kind supersedes
    """

    from mddbai.codec.section_meta import VALID_RELATION_KINDS  # noqa: PLC0415

    def _parse(ref: str) -> tuple[str, str, str]:
        if "#" not in ref:
            raise typer.BadParameter(
                f"reference must be <table>/<drawer>#<section>, got {ref!r}"
            )
        head, sid = ref.rsplit("#", 1)
        if "/" not in head:
            raise typer.BadParameter(
                f"reference must include table/drawer before '#', got {ref!r}"
            )
        table, drawer = head.split("/", 1)
        return table, drawer, sid

    if kind is not None and kind not in VALID_RELATION_KINDS:
        raise typer.BadParameter(
            f"--kind must be one of {VALID_RELATION_KINDS}, got {kind!r}"
        )

    a_t, a_d, a_s = _parse(a)
    b_t, b_d, b_s = _parse(b)

    a_target = f"{a_t}/{a_d}#{a_s}"
    b_target = f"{b_t}/{b_d}#{b_s}"

    with _open(data_dir) as db:
        # A -> B
        existing_a = db.get_section_meta(a_t, a_d, a_s) or {}
        if kind is None:
            a_related = list(existing_a.get("related") or [])
            if b_target not in a_related:
                a_related.append(b_target)
            db.put_section_meta(a_t, a_d, a_s, related=a_related, merge=True)
        else:
            a_relations = list(existing_a.get("relations") or [])
            if not any(
                r.get("target") == b_target and r.get("kind") == kind
                for r in a_relations
            ):
                a_relations.append({"target": b_target, "kind": kind})
            db.put_section_meta(a_t, a_d, a_s, relations=a_relations, merge=True)

        if bidir:
            existing_b = db.get_section_meta(b_t, b_d, b_s) or {}
            if kind is None:
                b_related = list(existing_b.get("related") or [])
                if a_target not in b_related:
                    b_related.append(a_target)
                db.put_section_meta(b_t, b_d, b_s, related=b_related, merge=True)
            else:
                b_relations = list(existing_b.get("relations") or [])
                if not any(
                    r.get("target") == a_target and r.get("kind") == kind
                    for r in b_relations
                ):
                    b_relations.append({"target": a_target, "kind": kind})
                db.put_section_meta(b_t, b_d, b_s, relations=b_relations, merge=True)

    suffix = f" [{kind}]" if kind else ""
    typer.echo(
        f"- linked: {a_target} {'<->' if bidir else '->'} {b_target}{suffix}"
    )


# ---------------------------------------------------------------------------
# 2026-05-09 reasoning primitives: conflict-check / compare / provenance / mutations
#
# These commands give the AI more raw signal to reason with at recall time.
# DB does not interpret meaning (D2) — it only collects signals from on-disk
# state and emits them. The AI judges what to do.
# ---------------------------------------------------------------------------


def _parse_section_ref(ref: str) -> tuple[str, str, str]:
    """``<table>/<drawer>#<section>`` -> (table, drawer, section). Raises typer.BadParameter."""

    if "#" not in ref:
        raise typer.BadParameter(
            f"reference must be <table>/<drawer>#<section>, got {ref!r}"
        )
    head, sid = ref.rsplit("#", 1)
    if "/" not in head:
        raise typer.BadParameter(
            f"reference must include table/drawer before '#', got {ref!r}"
        )
    table, drawer = head.split("/", 1)
    return table, drawer, sid


@app.command("conflict-check")
def conflict_check_cmd(
    data_dir: Path,
    ref: str = typer.Argument(
        ..., help="Section to check: <table>/<drawer>#<section>"
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Conflict signals for one section (2026-05-09).

    Inspects the section's metadata + outgoing edges and reports:

    - ``contradicts``: this section has a relations[kind=contradicts] edge.
    - ``mixed-kind``: this section has both refines and contradicts edges
      pointing to the same target (semantic mismatch).
    - ``stale-active``: this section's state=active, but another section
      points at it via relations[kind=supersedes] (something newer claims
      to replace it, yet it is still active).
    - ``state-mismatch``: this section's related/relations target has
      state=superseded or deprecated, but this section is active.
    - ``cycle-supersedes``: cycle detected when walking outgoing
      kind=supersedes edges back to this section.

    Returns rc 0 when no signals found, rc 1 when signals found.
    DB does not decide the meaning of these signals — the AI judges.
    """

    import json as _json  # noqa: PLC0415

    t, d, s = _parse_section_ref(ref)
    self_target = f"{t}/{d}#{s}"

    signals: list[dict[str, Any]] = []

    with _open(data_dir) as db:
        meta = db.get_section_meta(t, d, s)
        if meta is None:
            typer.echo(f"# error: section not found: {ref}", err=True)
            raise typer.Exit(2)

        relations = list(meta.get("relations") or [])
        related = list(meta.get("related") or [])
        my_state = meta.get("state")

        # Signal 1: direct contradicts edges.
        for r in relations:
            if r.get("kind") == "contradicts":
                signals.append({
                    "kind": "contradicts",
                    "target": r.get("target"),
                    "note": "this section explicitly contradicts target",
                })

        # Signal 2: mixed-kind edges to the same target.
        kinds_by_target: dict[str, set[str]] = {}
        for r in relations:
            tgt = r.get("target")
            k = r.get("kind")
            if not tgt or not k:
                continue
            kinds_by_target.setdefault(tgt, set()).add(k)
        for tgt, kinds in kinds_by_target.items():
            if "contradicts" in kinds and (kinds & {"refines", "implies", "depends-on"}):
                signals.append({
                    "kind": "mixed-kind",
                    "target": tgt,
                    "note": f"both contradicts and {sorted(kinds - {'contradicts'})} on same target",
                })

        # Signal 3: state-mismatch — outgoing edges land on superseded/deprecated.
        all_targets: list[tuple[str, str | None]] = []
        for x in related:
            all_targets.append((str(x), None))
        for r in relations:
            if r.get("target"):
                all_targets.append((str(r["target"]), r.get("kind")))
        for tgt, k in all_targets:
            if "#" not in tgt:
                continue
            head, ref_sid = tgt.rsplit("#", 1)
            if "/" not in head:
                continue
            ref_t, ref_d = head.split("/", 1)
            try:
                ref_meta = db.get_section_meta(ref_t, ref_d, ref_sid) or {}
            except Exception:  # noqa: BLE001
                ref_meta = {}
            ref_state = ref_meta.get("state")
            if ref_state in ("superseded", "deprecated") and my_state == "active":
                signals.append({
                    "kind": "state-mismatch",
                    "target": tgt,
                    "edge_kind": k,
                    "target_state": ref_state,
                    "note": f"active section points at {ref_state} target",
                })

        # Signal 4: stale-active — someone else marks this as superseded.
        if my_state == "active":
            try:
                tables = [str(name) for name in db.tables()]
            except Exception:  # noqa: BLE001
                tables = []
            data_root = Path(db.config.data_dir)
            for tname in tables:
                table_root = data_root / tname
                if not table_root.exists():
                    continue
                for path in table_root.rglob("*.md"):
                    if any(p.startswith("_") for p in path.relative_to(table_root).parts):
                        continue
                    if path.name.endswith((".lock", ".tmp")):
                        continue
                    try:
                        text = path.read_text(encoding="utf-8")
                    except OSError:
                        continue
                    if "supersedes" not in text and self_target not in text:
                        # Cheap pre-filter — file does not even mention us.
                        continue
                    rel = path.relative_to(table_root).as_posix()
                    if rel.endswith(".md"):
                        rel = rel[:-3]
                    other_meta_full = db.drawer_engine.cache.get_meta(path)
                    if not other_meta_full:
                        continue
                    try:
                        from mddbai.codec.section_meta import parse_sections_meta  # noqa: PLC0415
                        other_sections = parse_sections_meta(dict(other_meta_full))
                    except Exception:  # noqa: BLE001
                        continue
                    for o_sid, o_sm in other_sections.items():
                        for o_rel in o_sm.relations:
                            if o_rel.kind == "supersedes" and o_rel.target == self_target:
                                signals.append({
                                    "kind": "stale-active",
                                    "claimed_by": f"{tname}/{rel}#{o_sid}",
                                    "note": "another section says it supersedes us, but we are still active",
                                })

        # Signal 5: cycle-supersedes — outgoing supersedes chain returns to self.
        seen: set[str] = {self_target}
        frontier: list[str] = []
        for r in relations:
            if r.get("kind") == "supersedes" and r.get("target"):
                frontier.append(str(r["target"]))
        while frontier:
            cur = frontier.pop()
            if cur == self_target:
                signals.append({
                    "kind": "cycle-supersedes",
                    "via": list(seen),
                    "note": "supersedes chain cycles back to this section",
                })
                break
            if cur in seen or "#" not in cur:
                continue
            seen.add(cur)
            head, c_sid = cur.rsplit("#", 1)
            if "/" not in head:
                continue
            c_t, c_d = head.split("/", 1)
            try:
                cur_meta = db.get_section_meta(c_t, c_d, c_sid) or {}
            except Exception:  # noqa: BLE001
                cur_meta = {}
            for r in cur_meta.get("relations") or []:
                if r.get("kind") == "supersedes" and r.get("target"):
                    frontier.append(str(r["target"]))

    if json_out:
        typer.echo(_json.dumps({"section": self_target, "signals": signals}, ensure_ascii=False, indent=2))
    else:
        if not signals:
            typer.echo(f"- {self_target}: no conflict signals")
        else:
            typer.echo(f"- {self_target}: {len(signals)} signal(s)")
            for s in signals:
                head = f"  [{s['kind']}]"
                rest = " ".join(
                    f"{k}={v}" for k, v in s.items() if k != "kind"
                )
                typer.echo(f"{head} {rest}")

    if signals:
        raise typer.Exit(1)


@app.command("compare")
def compare_cmd(
    data_dir: Path,
    a: str = typer.Argument(..., help="A: <table>/<drawer>#<section>"),
    b: str = typer.Argument(..., help="B: <table>/<drawer>#<section>"),
    body_only: bool = typer.Option(
        True,
        "--body-only/--with-heading",
        help="Strip the heading line from each side before comparing (default).",
    ),
) -> None:
    """Compare two sections — emit common / a-only / b-only line sets.

    No semantic conclusion (D2). The AI reads the marked sets and judges
    what they mean. Output format::

        ## common
        line shared by both
        ## a-only
        line only in A
        ## b-only
        line only in B
    """

    a_t, a_d, a_s = _parse_section_ref(a)
    b_t, b_d, b_s = _parse_section_ref(b)

    with _open(data_dir) as db:
        a_text = db.take_section(a_t, a_d, a_s, body_only=body_only)
        b_text = db.take_section(b_t, b_d, b_s, body_only=body_only)

    if a_text is None:
        typer.echo(f"# error: A not found: {a}", err=True)
        raise typer.Exit(2)
    if b_text is None:
        typer.echo(f"# error: B not found: {b}", err=True)
        raise typer.Exit(2)

    a_lines = [ln.rstrip() for ln in a_text.splitlines() if ln.strip()]
    b_lines = [ln.rstrip() for ln in b_text.splitlines() if ln.strip()]
    a_set = set(a_lines)
    b_set = set(b_lines)

    common = [ln for ln in a_lines if ln in b_set]
    a_only = [ln for ln in a_lines if ln not in b_set]
    b_only = [ln for ln in b_lines if ln not in a_set]

    typer.echo("## common")
    if not common:
        typer.echo("(none)")
    for ln in common:
        typer.echo(ln)
    typer.echo("")
    typer.echo("## a-only")
    if not a_only:
        typer.echo("(none)")
    for ln in a_only:
        typer.echo(ln)
    typer.echo("")
    typer.echo("## b-only")
    if not b_only:
        typer.echo("(none)")
    for ln in b_only:
        typer.echo(ln)


@app.command("provenance")
def provenance_cmd(
    data_dir: Path,
    ref: str = typer.Argument(
        ..., help="Section: <table>/<drawer>#<section>"
    ),
    max_depth: int = typer.Option(
        5, "--max-depth", help="Maximum hops to walk back (cycle-safe)."
    ),
    kinds: str = typer.Option(
        "supersedes,refines,derived-from",
        "--kinds",
        help="Comma-separated relation kinds to follow back. Default covers ancestry kinds.",
    ),
) -> None:
    """Walk *outgoing* edges of the given kinds to surface ancestors.

    For section X, follows X's relations[kind in {supersedes,refines,derived-from}]
    transitively to expose where X comes from. Cycle-safe.

    AI uses this to trace why a decision is what it is. DB does not decide
    which ancestor "matters" (D2).
    """

    follow_kinds = {k.strip() for k in kinds.split(",") if k.strip()}
    if not follow_kinds:
        raise typer.BadParameter("--kinds must list at least one kind")

    t, d, s = _parse_section_ref(ref)
    start = f"{t}/{d}#{s}"

    chain: list[dict[str, Any]] = []
    visited: set[str] = {start}

    with _open(data_dir) as db:
        frontier: list[tuple[str, int, str | None]] = [(start, 0, None)]
        while frontier:
            cur, depth, via_kind = frontier.pop(0)
            if depth > max_depth:
                continue
            if cur != start:
                chain.append({
                    "ancestor": cur,
                    "depth": depth,
                    "via_kind": via_kind,
                })
            if "#" not in cur:
                continue
            head, c_sid = cur.rsplit("#", 1)
            if "/" not in head:
                continue
            c_t, c_d = head.split("/", 1)
            try:
                cur_meta = db.get_section_meta(c_t, c_d, c_sid) or {}
            except Exception:  # noqa: BLE001
                cur_meta = {}
            for r in cur_meta.get("relations") or []:
                k = r.get("kind")
                tgt = r.get("target")
                if not tgt or k not in follow_kinds:
                    continue
                if tgt in visited:
                    continue
                visited.add(str(tgt))
                frontier.append((str(tgt), depth + 1, k))

    typer.echo(f"- start: {start}")
    if not chain:
        typer.echo("  (no ancestors via " + ",".join(sorted(follow_kinds)) + ")")
    for entry in chain:
        typer.echo(
            f"  depth={entry['depth']} via={entry['via_kind']} "
            f"-> {entry['ancestor']}"
        )


@app.command("mutations")
def mutations_cmd(
    data_dir: Path,
    ref: str = typer.Argument(
        ..., help="Section: <table>/<drawer>#<section>"
    ),
) -> None:
    """List the revision chain of a section.

    Shows the section's own state / current_revision / supersedes labels.
    Then for each label in supersedes, attempts to find a sibling section
    in the same drawer matching ``<sid>:<label>`` or ``<sid>-<label>`` and
    reports its state. AI uses this to read the evolution of a decision.

    The DB does not interpret *why* the section evolved (D2) — only
    collects revision labels and their on-disk presence.
    """

    t, d, sid = _parse_section_ref(ref)
    self_target = f"{t}/{d}#{sid}"

    with _open(data_dir) as db:
        meta = db.get_section_meta(t, d, sid)
        if meta is None:
            typer.echo(f"# error: section not found: {ref}", err=True)
            raise typer.Exit(2)

        typer.echo(f"- section: {self_target}")
        typer.echo(f"  state: {meta.get('state') or '(unset)'}")
        typer.echo(f"  current_revision: {meta.get('current_revision') or '(unset)'}")
        supersedes = list(meta.get("supersedes") or [])
        if not supersedes:
            typer.echo("  supersedes: (none)")
            return
        typer.echo(f"  supersedes: {supersedes}")

        # Try to locate sibling revisions in the same drawer.
        try:
            sibling_ids = db.list_sections(t, d) or []
        except Exception:  # noqa: BLE001
            sibling_ids = []
        sibling_set = {str(x) for x in sibling_ids}

        for rev_label in supersedes:
            candidates = [
                f"{sid}:{rev_label}",
                f"{sid}-{rev_label}",
                f"{sid}_{rev_label}",
                str(rev_label),
            ]
            found = next((c for c in candidates if c in sibling_set), None)
            if found is None:
                typer.echo(f"  - {rev_label}: (sibling not found)")
                continue
            try:
                rev_meta = db.get_section_meta(t, d, found) or {}
            except Exception:  # noqa: BLE001
                rev_meta = {}
            typer.echo(
                f"  - {rev_label}: {t}/{d}#{found} "
                f"state={rev_meta.get('state') or '(unset)'}"
            )


@app.command("recall")
def recall_cmd(
    data_dir: Path,
    cue: str = typer.Argument(..., help="One-line natural-language cue (Korean/English fine)."),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Strict mode: succeeds only when there is *exactly one* section-level route. "
        "If ambiguous / missing, fails with an actionable message and no body dump. "
        "Also enabled by env MDDBAI_STRICT_RETRIEVAL=1 or MddbConfig.navigation_strict.",
    ),
    body_only: bool = typer.Option(
        False, "--body-only", help="Strip the H2 heading line and print body only."
    ),
    allow_large_dump: bool = typer.Option(
        False,
        "--allow-large-dump",
        help="Force-output even if the selected section exceeds the large threshold. Emits stderr warning.",
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON output."),
    max_routes: int = typer.Option(5, "--max-routes"),
    max_tables: int = typer.Option(5, "--max-tables"),
    max_drawers: int = typer.Option(20, "--max-drawers"),
    max_sections: int = typer.Option(50, "--max-sections"),
    include_superseded: bool = typer.Option(
        False,
        "--include-superseded",
        help="Include sections with state=superseded/deprecated. Default is active only.",
    ),
) -> None:
    """Strict navigation wrapper: navigate -> unique section route -> take.

    *Not* a search command. Takes the structural cue match from ``navigate``
    as-is; in strict mode it takes that section only when *exactly one*
    section-level route exists. Zero routes (none found) or 2+ routes
    (ambiguous) fail with an actionable message and no body dump. No automatic
    ranking/selection — when ambiguous the AI inspects candidates with
    ``mddbai navigate`` and then calls ``take``.

    Examples::

        mddbai recall .mddbai "why v1 was locked" --strict
        mddbai recall .mddbai "drawer model decision" --strict --json
    """

    with _open(data_dir) as db:
        config_strict = bool(getattr(db.config, "navigation_strict", False))
        is_strict = _strict.strict_active(flag=strict, config_strict=config_strict)

        result = db.navigate(
            cue,
            max_routes=max_routes,
            max_tables=max_tables,
            max_drawers=max_drawers,
            max_sections=max_sections,
            fallback_disabled=is_strict,
        )

        routes = list(result.get("routes") or [])
        # Keep only section-level routes (table/drawer-only fallback is not strict-eligible).
        section_routes = [
            r for r in routes if r.get("table") and r.get("drawer") and r.get("section")
        ]

        # state filter + conflict detection.
        state_by_route: dict[tuple[str, str, str], str | None] = {}
        for r in section_routes:
            try:
                meta = db.get_section_meta(
                    str(r["table"]), str(r["drawer"]), str(r["section"])
                ) or {}
            except Exception:  # noqa: BLE001
                meta = {}
            state_by_route[(str(r["table"]), str(r["drawer"]), str(r["section"]))] = (
                meta.get("state")
            )

        if not include_superseded:
            section_routes = [
                r
                for r in section_routes
                if state_by_route.get(
                    (str(r["table"]), str(r["drawer"]), str(r["section"]))
                )
                in (None, "active")
            ]

        # related state conflict detection: if the selected candidate's related
        # list contains superseded / deprecated entries, emit a one-line conflict signal.
        conflicts: list[str] = []
        for r in section_routes[:3]:
            try:
                sm = db.get_section_meta(
                    str(r["table"]), str(r["drawer"]), str(r["section"])
                )
            except Exception:  # noqa: BLE001
                sm = None
            if not sm:
                continue
            for ref in sm.get("related") or []:
                # ref format: "<table>/<drawer>#<section>" or "<table>/<drawer>"
                if "#" not in str(ref):
                    continue
                head, ref_sid = str(ref).rsplit("#", 1)
                if "/" not in head:
                    continue
                ref_t, ref_d = head.split("/", 1)
                try:
                    ref_meta = db.get_section_meta(ref_t, ref_d, ref_sid) or {}
                except Exception:  # noqa: BLE001
                    ref_meta = {}
                ref_state = ref_meta.get("state")
                if ref_state in ("superseded", "deprecated"):
                    conflicts.append(
                        f"{r['table']}/{r['drawer']}#{r['section']} -> "
                        f"{ref}: {ref_state}"
                    )

        outcome: dict[str, Any] = {
            "cue": cue,
            "strict": is_strict,
            "include_superseded": include_superseded,
            "selected": None,
            "candidates": section_routes,
            "warnings": list(result.get("warnings") or []),
            "conflicts": conflicts,
            "body": None,
        }

        if is_strict and not section_routes:
            outcome["error"] = (
                "strict recall failed: no section-level route. "
                "data lacks navigation signals or is too coarsely split; "
                "split/enrich required."
            )
            _emit_recall(outcome, json_output)
            raise typer.Exit(code=2)

        if is_strict and len(section_routes) > 1:
            preview = [
                f"{r['table']}/{r['drawer']}#{r['section']}" for r in section_routes[:5]
            ]
            outcome["error"] = (
                "strict recall failed: ambiguous — "
                f"{len(section_routes)} section-level candidates. "
                f"Use 'mddbai navigate' to disambiguate, then 'mddbai take'. "
                f"candidates: {preview}"
            )
            _emit_recall(outcome, json_output)
            raise typer.Exit(code=2)

        # Outside strict mode: if section_routes is empty, even the first
        # drawer/table-level candidate failed — don't dump body, only report candidates.
        if not section_routes:
            if not is_strict and routes:
                outcome["selected"] = routes[0]
                _emit_recall(outcome, json_output)
                return
            outcome["error"] = "no route found"
            _emit_recall(outcome, json_output)
            raise typer.Exit(code=1)

        chosen = section_routes[0]
        outcome["selected"] = chosen

        body = db.take_section(
            str(chosen["table"]),
            str(chosen["drawer"]),
            str(chosen["section"]),
            body_only=body_only,
        )
        if body is None:
            outcome["error"] = (
                "selected section disappeared between navigate and take "
                "(drawer/section mutated concurrently)"
            )
            _emit_recall(outcome, json_output)
            raise typer.Exit(code=1)

        size = len(body.encode("utf-8"))
        limit = _strict.large_section_threshold()
        if is_strict and size > limit and not allow_large_dump:
            outcome["error"] = (
                f"strict recall: selected section is too large ({size}B > {limit}B). "
                "Pass --allow-large-dump to override, or split the section finer."
            )
            outcome["body_size_bytes"] = size
            _emit_recall(outcome, json_output)
            raise typer.Exit(code=2)
        if is_strict and size > limit and allow_large_dump:
            _strict.warn_bypass(
                f"large section recall {chosen['table']}/{chosen['drawer']}#{chosen['section']}",
                size_bytes=size,
            )

        outcome["body"] = body
        outcome["body_size_bytes"] = size
        _emit_recall(outcome, json_output)


def _emit_recall(outcome: dict[str, Any], json_output: bool) -> None:
    """Display recall result (stderr message + stdout body, or JSON)."""

    if json_output:
        typer.echo(json.dumps(outcome, ensure_ascii=False, indent=2))
        return

    sel = outcome.get("selected")
    if sel and sel.get("section"):
        typer.echo(
            f"# selected: {sel.get('table')}/{sel.get('drawer')}#{sel.get('section')}",
            err=True,
        )
        reason = sel.get("reason")
        if reason:
            typer.echo(f"# reason: {reason}", err=True)
        signals = sel.get("signals") or []
        if signals:
            typer.echo(f"# signals: {','.join(signals)}", err=True)
    elif sel:
        typer.echo(
            f"# partial route: table={sel.get('table')} drawer={sel.get('drawer')}",
            err=True,
        )

    err = outcome.get("error")
    if err:
        typer.echo(f"error: {err}", err=True)

    for w in outcome.get("warnings", []):
        typer.echo(f"warning: {w}", err=True)

    # Conflict signal from the Meaning Reconstruction step — AI surfaces this to the user.
    for c in outcome.get("conflicts", []):
        typer.echo(f"# conflict: {c}", err=True)

    body = outcome.get("body")
    if body:
        typer.echo(body)


@app.command("lexicon-look")
def lexicon_look_cmd(
    data_dir: Path,
    cue: str = typer.Argument(..., help="One-line expression to look up (Korean/English fine)."),
    space: str = typer.Option(
        "both",
        "--space",
        help="Search space: user (user utterances) | ai (AI responses) | both (default).",
    ),
    top: int = typer.Option(5, "--top", help="Max results per space."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Find phrase nodes in past lexicon containing the cue — assists AI semantic inference.

    *No automatic matching*. Plain surface substring match only — the AI reads
    the results and infers *semantic cues* (D1 / D2 aligned). Used at step 4
    (Evidence Reading) of the 6-step recall flow.

    Examples::

        mddbai lexicon-look .mddbai "Gangneung" --space both --top 5
        mddbai lexicon-look .mddbai "decision" --space ai --json
    """

    from mddbai.brain.lexicon_store import (  # noqa: PLC0415
        LEXICON_SPACES,
        iter_all_nodes,
    )
    from mddbai.codec.frontmatter import parse as _fm_parse  # noqa: PLC0415

    if space == "both":
        spaces_to_check = list(LEXICON_SPACES)
    elif space in LEXICON_SPACES:
        spaces_to_check = [space]
    else:
        raise typer.BadParameter(
            f"--space must be one of user|ai|both, got {space!r}"
        )

    cue_norm = cue.strip().lower()
    if not cue_norm:
        raise typer.BadParameter("cue must be non-empty")

    out: dict[str, list[dict[str, Any]]] = {}
    for sp in spaces_to_check:
        hits: list[dict[str, Any]] = []
        for tier, path in iter_all_nodes(data_dir, space=sp):
            try:
                fm, _body = _fm_parse(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if fm.get("type") != "phrase":
                continue
            normalized = str(fm.get("normalized", "")).lower()
            surface = str(fm.get("surface", ""))
            if cue_norm not in normalized and cue_norm not in surface.lower():
                continue
            hits.append({
                "surface": surface,
                "normalized": normalized,
                "tier": tier,
                "seen_count": int(fm.get("seen_count", 0)),
                "strength": float(fm.get("strength", 0.0)),
                "last_seen": str(fm.get("last_seen", "")),
                "session_ids": list(fm.get("session_ids", []))[:3],
            })
        # tier priority (semantic > episodic > legacy) + strength desc
        tier_rank = {"semantic": 0, "episodic": 1, "legacy": 2}
        hits.sort(
            key=lambda h: (tier_rank.get(h["tier"], 9), -h["strength"], -h["seen_count"])
        )
        out[sp] = hits[:top]

    if json_output:
        typer.echo(json.dumps({"cue": cue, "results": out}, ensure_ascii=False, indent=2))
        return

    total = sum(len(v) for v in out.values())
    if total == 0:
        typer.echo(f"# no lexicon matches for {cue!r} in {','.join(spaces_to_check)}")
        return

    for sp in spaces_to_check:
        hits = out.get(sp, [])
        typer.echo(f"# {sp}_lexicon: {len(hits)} matches")
        for h in hits:
            typer.echo(
                f"  - {h['surface']!r} [{h['tier']}] "
                f"seen={h['seen_count']} strength={h['strength']:.2f} "
                f"last={h['last_seen'][:10]}"
            )


@app.command("refresh-summaries")
def refresh_summaries_cmd(
    data_dir: Path,
    table: str = typer.Argument(..., help="Table name"),
    overwrite_ai: bool = typer.Option(
        False,
        "--overwrite-ai",
        help="Also overwrite files with _authored_by: ai (default: preserve)",
    ),
) -> None:
    """Auto-refresh _summary.md stats for every folder in the table."""

    with _open(data_dir) as db:
        written = db.refresh_summaries(table, overwrite_ai=overwrite_ai)
    typer.echo(f"refreshed {len(written)} summaries")
    for p in written:
        typer.echo(f"  {p}")


@app.command("registry-add")
def registry_add_cmd(
    data_dir: Path,
    alias: str = typer.Argument(..., help="Short alias (e.g. dogma)"),
    canonical: str = typer.Argument(..., help="Canonical drawer path (e.g. decisions/dogma/core)"),
) -> None:
    """Register a drawer alias into _registry/drawers.md."""

    with _open(data_dir) as db:
        path = db.registry_add_alias(alias, canonical)
    typer.echo(f"registered: {alias} -> {canonical} ({path})")


@app.command("registry-remove")
def registry_remove_cmd(
    data_dir: Path,
    alias: str = typer.Argument(..., help="Alias to remove"),
) -> None:
    """Remove a drawer alias."""

    with _open(data_dir) as db:
        ok = db.registry_remove_alias(alias)
    if ok:
        typer.echo(f"removed: {alias}")
    else:
        typer.echo(f"not found: {alias}", err=True)
        raise typer.Exit(code=1)


@app.command("registry-list")
def registry_list_cmd(
    data_dir: Path,
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Full list of drawer aliases."""

    with _open(data_dir) as db:
        aliases = db.registry_list_aliases()
    if json_output:
        typer.echo(json.dumps(aliases, ensure_ascii=False, indent=2))
        return
    if not aliases:
        typer.echo("(empty registry)")
        return
    for entry in aliases:
        typer.echo(f"{entry['alias']} -> {entry['canonical']}")


@app.command("registry-overlaps")
def registry_overlaps_cmd(
    data_dir: Path,
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Signals for drawers whose same stem is placed in multiple locations (drift / overlap)."""

    with _open(data_dir) as db:
        overlaps = db.registry_overlaps()
    if json_output:
        typer.echo(json.dumps(overlaps, ensure_ascii=False, indent=2))
        return
    if not overlaps:
        typer.echo("(no overlapping drawer stems)")
        return
    for entry in overlaps:
        locs = entry.get("locations", [])
        typer.echo(f"{entry['stem']}: {len(locs)} locations")
        for loc in locs:
            typer.echo(f"  - {loc}")


@app.command("registry-suggest")
def registry_suggest_cmd(
    data_dir: Path,
    intended_drawer: str = typer.Argument(..., help="Drawer path you want to create"),
    table: str = typer.Option("", "--table", help="Search only within this table"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Suggest existing candidates (reuse recommendation) before creating a new drawer."""

    with _open(data_dir) as db:
        candidates = db.registry_suggest_reuse(intended_drawer, table=table or None)
    if json_output:
        typer.echo(json.dumps(candidates, ensure_ascii=False, indent=2))
        return
    if not candidates:
        typer.echo("(no reuse candidates)")
        return
    for c in candidates:
        typer.echo(c)


@app.command()
def flush(
    data_dir: Path,
    table: str = typer.Option(
        "", "--table", help="Flush only this table. Empty = all."
    ),
) -> None:
    """Memtable -> SSTable + drawer dirty buffer -> disk.

    Stage Z.7++ (2026-05-03): with drawer write-back introduced, ``put_section``
    no longer writes to disk immediately. It writes at the explicit call site.
    Inside a ``with _open`` block, ``__exit__`` flushes automatically, so
    one-shot CLI usage is unaffected.
    """

    with _open(data_dir) as db:
        db.flush(table or None)  # type: ignore[arg-type]
    typer.echo("ok")


@app.command()
def doctor(
    data_dir: Path = typer.Argument(..., help="MDDB data directory"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
    gate: bool = typer.Option(
        False,
        "--gate",
        help=(
            "Gate mode — only checks the critical, code-enforceable spots "
            "(palace identity / flat-dump drawer / 1MiB hard cap). Used in "
            "pre-commit / CI. One ERROR -> rc 2."
        ),
    ),
) -> None:
    """AI-friendliness self-check. Non-zero exit code on errors.

    ``--gate`` mode is separate — checks only code-enforceable on-disk spots.
    """

    from mddbai.obs.doctor import check, gate_check  # noqa: PLC0415

    with _open(data_dir) as db:
        report = gate_check(db) if gate else check(db)

    def _as_dict(issue: Any) -> dict[str, Any]:
        return {
            "code": issue.code,
            "severity": issue.severity,
            "target": issue.target,
            "message": issue.message,
        }

    if json_output:
        payload: dict[str, Any] = {
            "data_dir": report.data_dir,
            "tables": report.tables,
            "errors": [_as_dict(issue) for issue in report.errors],
            "warnings": [_as_dict(issue) for issue in report.warnings],
            "info": [_as_dict(issue) for issue in report.info],
            "healthy": report.is_healthy(),
            "mode": "gate" if gate else "full",
        }
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if gate:
            typer.echo("# mddbai doctor --gate")
            typer.echo("")
        typer.echo(report.summary())

    if not report.is_healthy():
        # Gate mode returns rc 2 explicitly (distinct from pre-commit/CI block); full mode returns rc 1.
        raise typer.Exit(code=2 if gate else 1)


@app.command("write-summary")
def write_summary_cmd(
    data_dir: Path = typer.Argument(..., help="MDDB data directory"),
    target: str = typer.Argument(
        ..., help="Folder relative to data_dir (e.g. notes/2026/04/29)"
    ),
    content: str = typer.Option(
        "", "--content", help="Summary body. If empty, use --from-stdin."
    ),
    from_stdin: bool = typer.Option(
        False, "--from-stdin", help="Read body from standard input"
    ),
    authored_by: str = typer.Option(
        "ai", "--authored-by", help="frontmatter _authored_by value"
    ),
) -> None:
    """The AI directly overwrites a folder's _summary.md (H.1 delegated API)."""

    import sys  # noqa: PLC0415

    if from_stdin:
        body = sys.stdin.read()
    else:
        body = content
    if not body:
        typer.echo("error: content is empty (provide --content or --from-stdin)", err=True)
        raise typer.Exit(code=2)

    with _open(data_dir) as db:
        path = db.write_summary(target, body, authored_by=authored_by)
    typer.echo(path.as_posix())


# ---------------------------------------------------------------------------
# mddbai cluster (sub-command) — Stage L.10: cluster disk layout
# ---------------------------------------------------------------------------

cluster_app = typer.Typer(
    help="Cluster meta management (list / create / stats / move). "
    "MDDB does not auto-place — the AI calls this explicitly.",
    no_args_is_help=True,
)


@cluster_app.command("list")
def cluster_list_cmd(
    data_dir: Path = typer.Argument(...),
    table: str = typer.Argument(..., help="Table name"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Print every cluster in ``<table>/_clusters/`` with member_count."""

    from mddbai.brain.clusters import count_cluster_members, iter_clusters  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    for c in iter_clusters(data_dir, table):
        members = count_cluster_members(data_dir, table, c.id)
        out.append(
            {
                "id": c.id,
                "label": c.label,
                "members": members,
                "created_ns": c.created_ns,
                "last_accessed_ns": c.last_accessed_ns,
            }
        )
    if json_output:
        typer.echo(json.dumps(out, indent=2, ensure_ascii=False))
        return
    if not out:
        typer.echo("(no clusters)")
        return
    for row in out:
        typer.echo(f"{row['id']}\t{row['members']}\t{row['label']}")


@cluster_app.command("create")
def cluster_create_cmd(
    data_dir: Path = typer.Argument(...),
    table: str = typer.Argument(..., help="Table name"),
    cluster_id: str = typer.Argument(..., help="New cluster ID"),
    label: str = typer.Option("", "--label", help="Cluster label (human-readable)"),
) -> None:
    """Create an empty cluster. The AI explicitly decides the cluster ID and label."""

    from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415

    from mddbai.brain.clusters import Cluster, upsert_cluster, write_manifest  # noqa: PLC0415

    now_ns = int(_dt.now(tz=_tz.utc).timestamp() * 1_000_000_000)
    c = Cluster(
        id=cluster_id,
        label=label,
        created_ns=now_ns,
        last_accessed_ns=now_ns,
        member_count=0,
    )
    upsert_cluster(data_dir, table, c)
    write_manifest(data_dir, table)
    typer.echo(f"created cluster {cluster_id}")


@cluster_app.command("stats")
def cluster_stats_cmd(
    data_dir: Path = typer.Argument(...),
    table: str = typer.Argument(..., help="Table name"),
    cluster_id: str = typer.Argument(..., help="cluster ID"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Print metadata for a single cluster."""

    from mddbai.brain.clusters import count_cluster_members, load_cluster  # noqa: PLC0415

    c = load_cluster(data_dir, table, cluster_id)
    if c is None:
        typer.echo(f"error: cluster not found: {cluster_id}", err=True)
        raise typer.Exit(code=1)
    members = count_cluster_members(data_dir, table, cluster_id)
    payload = {
        "id": c.id,
        "label": c.label,
        "members": members,
        "created_ns": c.created_ns,
        "last_accessed_ns": c.last_accessed_ns,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for k, v in payload.items():
            typer.echo(f"{k}: {v}")


app.add_typer(cluster_app, name="cluster")


# ---------------------------------------------------------------------------
# mddbai palace (sub-command) — Stage AA.3.1: palace skeleton init, interactive 4 questions
# ---------------------------------------------------------------------------

palace_app = typer.Typer(
    help="Palace skeleton management. `palace init` writes INDEX.md via 4 Q&A. "
    "MDDB only provides the space; the AI builds the skeleton itself.",
    no_args_is_help=True,
)


@palace_app.command("init")
def palace_init_cmd(
    data_dir: Path = typer.Argument(..., help="Data directory"),
    table: str = typer.Argument(..., help="Palace (table) name"),
    purpose: str = typer.Option(
        "", "--purpose", help="One-line purpose of this palace (empty -> interactive in TTY)"
    ),
    scale: str = typer.Option(
        "", "--scale", help="Expected scale 100/1k/10k/100k/1M+ (empty -> interactive in TTY)"
    ),
    axes: str = typer.Option(
        "",
        "--axes",
        help="Natural classification axes. Comma-separated from path,time,topic,person,free (empty -> interactive in TTY)",
    ),
    fallback: str = typer.Option(
        "", "--fallback", help="auto_create or unsorted (empty -> interactive in TTY)"
    ),
    responsibilities_json: str = typer.Option(
        "",
        "--responsibilities-json",
        help="JSON dict of one-line responsibility per folder. e.g. '{\"records\":\"raw\",\"_index\":\"index\"}'. "
        "If unset + TTY: prompt; if unset + non-TTY: fall back to empty dict.",
    ),
    yes: bool = typer.Option(
        False, "-y", "--yes", help="Skip confirmation prompt (for CI/scripts)"
    ),
    no_confirm: bool = typer.Option(
        False,
        "--no-confirm",
        help="Synonym for `-y`. Explicit short form. For cold AI / sub-agent / CI automation.",
    ),
) -> None:
    """4 questions + per-folder responsibilities -> writes INDEX.md.

    K.1 (2026-05-05) — non-interactive automation works. Pass all 4 options +
    responsibility dict and it runs end-to-end with empty stdin. Entry point
    for cold AI / sub-agent / CI.

    Interactive flow (TTY + missing options only):
        1) one-line purpose
        2) expected scale
        3) natural classification axes (comma-separated)
        4) fallback policy
        -> mddbai prints a skeleton draft
        -> enter one-line responsibility per folder (skip with --responsibilities-json)
        -> confirm and write INDEX.md (skip with --yes/--no-confirm)

    Non-interactive call (automation):
        mddbai palace init <data> <table> \\
            --purpose "..." --scale "1k" --axes "path,topic" --fallback "auto_create" \\
            --responsibilities-json '{"records": "raw"}' --no-confirm
    """

    from mddbai.brain.palace_init import (  # noqa: PLC0415
        VALID_AXES,
        VALID_FALLBACKS,
        VALID_SCALES,
    )

    is_tty = sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else False

    if not purpose:
        if is_tty:
            purpose = typer.prompt("1) Purpose of this palace (one line)")
        else:
            typer.echo(
                "error: --purpose missing in non-TTY. automation calls must pass --purpose explicitly.",
                err=True,
            )
            raise typer.Exit(code=2)
    scale = scale or (
        typer.prompt(
            f"2) Expected scale ({'/'.join(sorted(VALID_SCALES))})", default="1k"
        )
        if is_tty
        else "1k"
    )
    axes = axes or (
        typer.prompt(
            f"3) Natural axes (comma-separated, {','.join(sorted(VALID_AXES))})",
            default="path,topic",
        )
        if is_tty
        else "path,topic"
    )
    fallback = fallback or (
        typer.prompt(
            f"4) fallback ({'/'.join(sorted(VALID_FALLBACKS))})",
            default="auto_create",
        )
        if is_tty
        else "auto_create"
    )

    axes_tuple = tuple(a.strip() for a in axes.split(",") if a.strip())

    # Parse --responsibilities-json. If unset + TTY -> prompt; if unset + non-TTY -> empty dict.
    responsibilities: dict[str, str] = {}
    if responsibilities_json:
        try:
            parsed = json.loads(responsibilities_json)
        except json.JSONDecodeError as exc:
            typer.echo(f"error: failed to parse --responsibilities-json: {exc}", err=True)
            raise typer.Exit(code=2) from None
        if not isinstance(parsed, dict):
            typer.echo(
                "error: --responsibilities-json must be a JSON object (dict).",
                err=True,
            )
            raise typer.Exit(code=2)
        responsibilities = {str(k): str(v) for k, v in parsed.items()}

    data_dir.mkdir(parents=True, exist_ok=True)
    with _open(data_dir) as db:
        try:
            draft = db.init_palace(
                table,
                purpose=purpose,
                scale=scale,
                axes=axes_tuple,
                fallback=fallback,
            )
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from None

        typer.echo("\n[mddbai] skeleton draft:")
        for f in draft.folders:
            typer.echo(f"  - {f.name}/")
        typer.echo("  decision rules:")
        for r in draft.decision_rules:
            typer.echo(f"    - {r}")

        # Per-folder responsibilities — if JSON not given and TTY, prompt; otherwise keep empty dict.
        if not responsibilities and is_tty:
            typer.echo("\n[mddbai] one-line responsibility per folder (empty = none):")
            for f in draft.folders:
                resp = typer.prompt(f"  {f.name}/", default="")
                if resp:
                    responsibilities[f.name] = resp

        skip_confirm = yes or no_confirm or not is_tty
        if not skip_confirm:
            ok = typer.prompt(
                "\n[mddbai] write INDEX.md with the skeleton above? [y/N]", default="N"
            )
            if ok.strip().lower() not in {"y", "yes"}:
                typer.echo("aborted.")
                raise typer.Exit(code=1)

        idx_path = db.confirm_init_palace(table, draft, responsibilities)
        typer.echo(f"\n[mddbai] INDEX.md written: {idx_path}")
        for f in draft.folders:
            typer.echo(f"  folder created: {data_dir / table / f.name}")


@palace_app.command("root-init")
def palace_root_init_cmd(
    data_dir: Path = typer.Argument(..., help="Data directory"),
    purpose: str = typer.Option(
        "", "--purpose", help="One-line purpose of this palace (empty -> interactive in TTY)"
    ),
    scale: str = typer.Option(
        "", "--scale", help="Expected scale 100/1k/10k/100k/1M+ (empty -> interactive in TTY)"
    ),
    axes: str = typer.Option(
        "",
        "--axes",
        help="Natural classification axes. Comma-separated from path,time,topic,person,free (empty -> interactive in TTY)",
    ),
    fallback: str = typer.Option(
        "", "--fallback", help="auto_create or unsorted (empty -> interactive in TTY)"
    ),
    yes: bool = typer.Option(
        False, "-y", "--yes", help="Skip confirmation prompt"
    ),
    no_confirm: bool = typer.Option(
        False,
        "--no-confirm",
        help="Synonym for `-y`. For cold AI / sub-agent / CI automation.",
    ),
) -> None:
    """Write palace root identity to data_dir/_palace.md.

    Does not write _INDEX.md. The disk ls *is* the index.
    Idempotent: same content twice = OK; different content = error.

    Non-interactive call (automation):
        mddbai palace root-init <data> \\
            --purpose "..." --scale "10k" --axes "topic,time" --fallback "auto_create" \\
            --no-confirm
    """
    from mddbai.brain.palace_root import (  # noqa: PLC0415
        VALID_AXES,
        VALID_FALLBACKS,
        VALID_SCALES,
    )
    from mddbai.core.errors import ConflictError  # noqa: PLC0415

    is_tty = sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else False

    if not purpose:
        if is_tty:
            purpose = typer.prompt("1) Purpose of this palace (one line)")
        else:
            typer.echo(
                "error: --purpose missing in non-TTY. automation calls must pass --purpose explicitly.",
                err=True,
            )
            raise typer.Exit(code=2)
    scale = scale or (
        typer.prompt(
            f"2) Expected scale ({'/'.join(sorted(VALID_SCALES))})", default="1k"
        )
        if is_tty
        else "1k"
    )
    axes = axes or (
        typer.prompt(
            f"3) Natural axes (comma-separated, {','.join(sorted(VALID_AXES))})",
            default="path,topic",
        )
        if is_tty
        else "path,topic"
    )
    fallback = fallback or (
        typer.prompt(
            f"4) fallback ({'/'.join(sorted(VALID_FALLBACKS))})",
            default="auto_create",
        )
        if is_tty
        else "auto_create"
    )

    axes_tuple = tuple(a.strip() for a in axes.split(",") if a.strip())

    skip_confirm = yes or no_confirm or not is_tty
    if not skip_confirm:
        ok = typer.prompt(
            f"\n[mddbai] write _palace.md? purpose={purpose!r} scale={scale!r} [y/N]",
            default="N",
        )
        if ok.strip().lower() not in {"y", "yes"}:
            typer.echo("aborted.")
            raise typer.Exit(code=1)

    data_dir.mkdir(parents=True, exist_ok=True)
    with _open(data_dir) as db:
        try:
            palace_path = db.init_palace_root(
                purpose=purpose,
                scale=scale,
                axes=axes_tuple,
                fallback=fallback,
            )
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from None
        except ConflictError as exc:
            typer.echo(f"error (conflict): {exc}", err=True)
            raise typer.Exit(code=3) from None

    typer.echo(f"[mddbai] _palace.md written: {palace_path}")
    typer.echo(f"  purpose : {purpose}")
    typer.echo(f"  scale   : {scale}")
    typer.echo(f"  axes    : {', '.join(axes_tuple)}")
    typer.echo(f"  fallback: {fallback}")


@palace_app.command("status")
def palace_status_cmd(
    data_dir: Path = typer.Argument(...),
    table: str = typer.Argument(..., help="Table name"),
) -> None:
    """Print INDEX.md presence + frontmatter."""

    with _open(data_dir) as db:
        if not db.has_palace_index(table):
            typer.echo(f"[mddbai] {table}: INDEX.md missing. call `mddbai palace init`.")
            raise typer.Exit(code=1)
        got = db.read_palace_index(table)
        assert got is not None
        front = got["front"]
        typer.echo(f"[mddbai] {table} palace:")
        typer.echo(f"  purpose: {front.get('purpose')}")
        typer.echo(f"  scale: {front.get('scale')}")
        typer.echo(f"  axes: {front.get('axes')}")
        typer.echo(f"  fallback: {front.get('fallback')}")
        typer.echo(f"  path: {got['path']}")


app.add_typer(palace_app, name="palace")


# ---------------------------------------------------------------------------
# Stage U (2026-05-02) — explicit CLI entry points pulled out of sleep
# Stage W: removed archive / index-refresh / stats-dormant / stats-cluster (record/shard deprecated)
# ---------------------------------------------------------------------------


@app.command("homeostasis")
def homeostasis_cmd(
    data_dir: Path = typer.Argument(..., help="MDDB data directory"),
    table: str | None = typer.Option(None, "--table", help="Target table (omit = all)"),
) -> None:
    """Stage U.4 — explicit call to absorb external edits.

    The sleep cycle's ``HomeostasisTask`` uses Q.8 dirty-tracking to reconcile
    only changed folders. Call this explicitly when you want to sync quickly
    after editing many .md files with external tools.
    """

    from mddbai.brain.homeostasis import homeostasis  # noqa: PLC0415

    with _open(data_dir) as db:
        tables = [TableName(table)] if table else db.tables()
        out: list[dict[str, Any]] = []
        for t in tables:
            report = homeostasis(db, t)
            out.append({
                "table": str(t),
                "cells_absorbed": report.cells_absorbed,
                "cells_removed": report.cells_removed,
                "cells_reconciled": report.cells_reconciled,
                "parse_failures": report.parse_failures,
            })
        typer.echo(json.dumps({"homeostasis": out}))


# ---------------------------------------------------------------------------
# mddbai hook (sub-command) — toggle Claude Code SessionStart hooks
# ---------------------------------------------------------------------------

hook_app = typer.Typer(
    help="Manage Claude Code 4 hooks (enable/disable/status). "
    "For non-Claude-Code tools use rule files (`mddbai init --write-rules-snippet=<tool>`).",
    no_args_is_help=True,
)
app.add_typer(hook_app, name="hook")


@hook_app.command("enable")
def hook_enable(
    data_dir: Path = typer.Argument(..., help="MDDB data directory"),
    project_root: Path | None = typer.Option(
        None, "--project-root", help="Target project root. Default: parent of data_dir"
    ),
) -> None:
    """Install / update the Claude Code 4 hooks (SessionStart / PreToolUse / UserPromptSubmit / Stop) and skills."""

    from mddbai.cli import integration as _intg  # noqa: PLC0415

    root = (project_root or data_dir.resolve().parent).resolve()
    try:
        data_rel = data_dir.resolve().relative_to(root).as_posix()
    except ValueError:
        data_rel = data_dir.as_posix()

    full = _intg.install_claude_full(root)
    typer.echo(
        f"python hooks: {len(full['hooks'])} files -> .claude/hooks/mddbai_*.py [installed]"
    )
    typer.echo(
        f"skills: {len(full['skills'])} files -> .claude/skills/mddbai-*/SKILL.md [installed]"
    )
    typer.echo(f"settings: {full['settings']} [{full['settings_status']}]")
    typer.echo(f"data_rel: {data_rel}")


@hook_app.command("disable")
def hook_disable(
    project_root: Path = typer.Argument(
        ..., help="Target project root (where settings.json lives)"
    ),
    keep_script: bool = typer.Option(
        False,
        "--keep-script",
        help="Keep hook script files (default: delete them together)",
    ),
) -> None:
    """Remove mddbai hook entries and installed files from the Claude Code 4 hooks.

    Removes the 4 mddbai hook entries from settings.json and, by default,
    deletes ``.claude/hooks/mddbai_*.py`` + ``.claude/skills/mddbai-*/``
    as well. Use ``--keep-script`` to preserve the files.
    """

    from mddbai.cli import integration as _intg  # noqa: PLC0415

    full = _intg.uninstall_claude_full(
        project_root.resolve(), delete_files=not keep_script
    )
    typer.echo(f"settings entries removed: {full['removed_count']}")
    if full["settings"]:
        typer.echo(f"settings: {full['settings']}")
    if not keep_script:
        typer.echo("hook scripts + skills: deleted")


@hook_app.command("status")
def hook_status(
    project_root: Path = typer.Argument(..., help="Target project root"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Report current install state of the Claude Code 4 hooks + skills."""

    from mddbai.cli import integration as _intg  # noqa: PLC0415

    info = _intg.claude_full_status(project_root.resolve())
    if json_output:
        payload: dict[str, Any] = {}
        for k, v in info.items():
            if isinstance(v, Path):
                payload[k] = str(v)
            elif isinstance(v, dict):
                payload[k] = {kk: (str(vv) if isinstance(vv, Path) else vv) for kk, vv in v.items()}
            else:
                payload[k] = v
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    if info["installed"]:
        typer.echo("installed: yes (all 4 hooks + 3 skills + settings registered)")
    else:
        typer.echo("installed: no (some missing)")
    typer.echo(f"  hooks: {info['hooks']}")
    typer.echo(f"  skills: {info['skills']}")
    typer.echo(f"  in_settings: {info['in_settings']}")
    typer.echo(f"  settings: {info['settings']}")


# ---------------------------------------------------------------------------
# mddbai demo — J.3.3 5-minute wow (one command: seed + recall demo)
# ---------------------------------------------------------------------------


@app.command("demo")
def demo_cmd(
    data_dir: Path = typer.Argument(
        Path(".mddbai"),
        help="Demo data directory. Default .mddbai (single-palace standard — install-layout.md)",
    ),
    keep: bool = typer.Option(
        False, "--keep", help="Do not delete the data directory after run"
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Show mddbai's core flow in 30 seconds (5 seeds -> recall simulation).

    Zero external APIs, zero LLM calls. Even without AI tools you can see
    *what the disk looks like*.
    """

    from mddbai.brain.sleep import SleepRunner, default_tasks  # noqa: PLC0415

    if data_dir.exists():
        if not json_output:
            typer.echo(f"warning: {data_dir} already exists. running demo on existing data.")
    else:
        data_dir.mkdir(parents=True)

    # 1) init (quiet)
    with _open(data_dir):
        pass

    # 2) 5 seeds (different dates + different tables + English bodies)
    seeds = [
        ("notes", "darkmode", "2026-04-28T10:00:00Z",
         "Add dark mode toggle. Follow the system prefers-color-scheme.",
         {"tags": ["ui", "darkmode"], "title": "Dark mode toggle"}),
        ("notes", "kbd-shortcut", "2026-04-28T14:00:00Z",
         "Open quick search with Cmd+K. Common pattern.",
         {"tags": ["ui", "shortcut"], "title": "Keyboard shortcut"}),
        ("decisions", "stack-python", "2026-04-29T09:00:00Z",
         "Backend is Python 3.11+. Use FastAPI only where async is needed.",
         {"tags": ["arch"], "title": "Stack decision - Python"}),
        ("meetings", "weekly-2026-04-30", "2026-04-30T10:00:00Z",
         "Weekly meeting: dark mode ships next week. KB shortcut deferred.",
         {"tags": ["meeting"], "title": "Weekly meeting"}),
        ("tasks", "darkmode-rollout", "2026-04-30T11:00:00Z",
         "Dark mode gradual rollout. 5% -> 50% -> 100%.",
         {"status": "todo", "tags": ["darkmode"], "title": "Dark mode rollout"}),
    ]

    with _open(data_dir) as db:
        for table, key, _iso, body, meta in seeds:
            # drawer = key (slug), section = "main"
            # Prepend meta as frontmatter to preserve it inline
            import yaml as _yaml  # noqa: PLC0415
            meta_block = f"---\n{_yaml.dump(meta, allow_unicode=True, default_flow_style=False).strip()}\n---\n"
            db.put_section(table, key, "main", meta_block + body)
        db.flush()

        # Manually create _summary.md (call write_summary directly instead of running sleep cycle)
        for table in {s[0] for s in seeds}:
            db.write_summary(
                table,
                f"# {table} summary\n\nDemo seed data — AI memory palace example.",
            )

        # One sleep run (summaries / indexes / attention). brain_auto follows config.
        runner = SleepRunner(db, default_tasks(brain_auto=db.config.brain_auto))
        sleep_results = runner.run_once(now_ns=int(db.now))

    sleep_ok = sum(1 for r in sleep_results if r.ok)
    sleep_total = len(sleep_results)

    # 3) Recall simulation — how the AI reads to get the answer
    recall_paths = [
        data_dir / "notes" / "_summary.md",
        data_dir / "notes" / "darkmode.md",
        data_dir / "notes" / "kbd-shortcut.md",
    ]
    recall_paths = [p for p in recall_paths if p.exists()]

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "data_dir": data_dir.as_posix(),
                    "seeds": len(seeds),
                    "sleep_ok": f"{sleep_ok}/{sleep_total}",
                    "recall_paths": [p.as_posix() for p in recall_paths],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo("")
        typer.echo("=" * 60)
        typer.echo("[mddbai demo] seed done — 5 records placed")
        typer.echo("=" * 60)
        typer.echo(f"  data root: {data_dir.as_posix()}")
        typer.echo(f"  seed records: {len(seeds)} (notes/decisions/meetings/tasks)")
        typer.echo(f"  sleep job: {sleep_ok}/{sleep_total} ok")
        typer.echo("")
        typer.echo("[mddbai demo] AI recall flow (direct read, 0 LLM calls)")
        typer.echo("-" * 60)
        typer.echo("  user: \"show me yesterday's dark mode note\"")
        typer.echo("  AI:")
        for p in recall_paths[:5]:
            try:
                rel = p.relative_to(data_dir).as_posix()
            except ValueError:
                rel = p.as_posix()
            typer.echo(f"    Read {data_dir.name}/{rel}")
        typer.echo("    -> found dark-mode toggle note")
        typer.echo("")
        typer.echo("[mddbai demo] try directly with these commands")
        typer.echo("-" * 60)
        typer.echo(f"  ls {data_dir}/notes/2026/04/28/")
        typer.echo(f"  cat {data_dir}/notes/_summary.md")
        typer.echo(f"  mddbai scan {data_dir} notes")
        typer.echo(f"  mddbai doctor {data_dir}")
        typer.echo("")

    # Safety first: data is not auto-deleted; user cleans up.
    # (--keep reserved for future extension; for now data is *always* kept.)
    _ = keep


# ---------------------------------------------------------------------------
# mddbai capture (sub-command) — K.1.1 raw utterance capture
# ---------------------------------------------------------------------------

capture_app = typer.Typer(
    help="Raw data capture (called by UserPromptSubmit hook etc.). "
    "PII masking + saved to disk under _inbox/<kind>/.",
    no_args_is_help=True,
)
app.add_typer(capture_app, name="capture")


@capture_app.command("utterance")
def capture_utterance_cmd(
    data_dir: Path = typer.Argument(..., help="MDDB data directory"),
    text: str = typer.Option(
        "", "--text", help="Utterance text. Empty -> use stdin or --from-claude-hook."
    ),
    raw_stdin: bool = typer.Option(
        False, "--raw-stdin", help="Read raw text from stdin"
    ),
    from_claude_hook: bool = typer.Option(
        False,
        "--from-claude-hook",
        help="Parse Claude Code hook JSON input (stdin) — extract prompt + session_id",
    ),
    session_id: str = typer.Option(
        "default", "--session-id", help="Session identifier"
    ),
    turn_idx: int | None = typer.Option(
        None, "--turn-idx", help="Turn index within the same session"
    ),
    source: str = typer.Option(
        "user_prompt_submit",
        "--source",
        help="Capture origin (audit metadata)",
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Mask PII in a raw user utterance and save it to ``_inbox/utterance/<ulid>.md`` (K.1.1)."""

    import sys as _sys  # noqa: PLC0415
    from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415

    from mddbai.brain import utterance as _utt  # noqa: PLC0415

    raw_text = text
    sid = session_id

    if from_claude_hook:
        # Claude Code hook protocol: JSON on stdin
        # {"hook_event_name":"UserPromptSubmit","session_id":"...","prompt":"..."}
        try:
            payload = json.loads(_sys.stdin.read() or "{}")
        except json.JSONDecodeError as exc:
            typer.echo(f"error: invalid hook JSON: {exc}", err=True)
            raise typer.Exit(code=2) from None
        if isinstance(payload, dict):
            raw_text = str(payload.get("prompt", "")) or raw_text
            if isinstance(payload.get("session_id"), str):
                sid = payload["session_id"]
    elif raw_stdin:
        raw_text = _sys.stdin.read()

    if not raw_text.strip():
        # Ignore empty utterances (not an error — keeps the hook safe)
        if json_output:
            typer.echo(json.dumps({"skipped": "empty"}, ensure_ascii=False))
        return

    ts_ns = int(_dt.now(tz=_tz.utc).timestamp() * 1_000_000_000)
    result = _utt.capture_utterance(
        data_dir,
        raw_text,
        session_id=sid,
        ts_ns=ts_ns,
        turn_idx=turn_idx,
        source=source,
    )

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "path": result.path.as_posix(),
                    "id": result.utterance_id,
                    "redactions": result.redactions,
                },
                ensure_ascii=False,
            )
        )
    else:
        typer.echo(f"captured: {result.path.as_posix()}")
        if result.redactions:
            redacted = ", ".join(f"{k}={v}" for k, v in result.redactions.items())
            typer.echo(f"  redacted: {redacted}")


# ---- write / read — placement assist (2026-05-07, 3-call flow T.2~T.5) ----
#
# Essence of write/read: mddbai only suggests *location*. Meaning is decided
# by the AI/user. Output is all Markdown (saved data / candidates / results /
# logs). No JSON option.
#
# 3-call flow for write:
#   Call 1: no destination option -> dump disk map (rc 0, no save)
#   Call 2: destination option set but no --yes -> location review + format guide (rc 0, no save)
#   Call 3: --yes -> save + recall-check (weak/miss also saved, stderr warning only)


def _write_call1_map(
    data_dir: Path,
    *,
    cues: list[str],
    entities: list[str],
    kind_norm: str | None,
    max_routes: int,
) -> None:
    """Call 1: dump disk map (palace + folder list + cue candidate drawers + 1/2/3 options)."""

    from mddbai.cli import _placement as _p  # noqa: PLC0415
    from mddbai.brain.palace_root import has_palace_root, read_palace_root  # noqa: PLC0415

    typer.echo("# mddbai write — call 1: disk map")
    typer.echo("")

    # palace identity
    typer.echo("## palace")
    if has_palace_root(data_dir):
        cfg = read_palace_root(data_dir)
        if cfg is not None:
            typer.echo(f"- purpose: {cfg.purpose}")
            typer.echo(f"- scale: {cfg.scale}")
            typer.echo(f"- axes: {', '.join(cfg.axes)}")
            typer.echo(f"- fallback: {cfg.fallback}")
        else:
            typer.echo("- purpose: (unset)")
    else:
        typer.echo("- purpose: (unset — recommended: init via 'mddbai palace init <data_dir>')")
    typer.echo("")

    # Existing folders (tables)
    typer.echo("## existing folders (tables)")
    _SYSTEM_PREFIXES = {"_wal", "_brain", "_self"}
    from mddbai.engine import _is_skippable_dir_name  # noqa: PLC0415

    tables: list[Path] = []
    if data_dir.exists():
        for p in sorted(data_dir.iterdir()):
            if p.is_dir() and not _is_skippable_dir_name(p.name) and p.name not in _SYSTEM_PREFIXES:
                tables.append(p)
    if not tables:
        typer.echo("(none — first entry)")
    else:
        for tbl in tables:
            drawers = [d for d in tbl.rglob("*.md") if not d.name.startswith("_")]
            sections_count = 0
            total_bytes = 0
            for md in drawers:
                try:
                    sz = md.stat().st_size
                    total_bytes += sz
                except OSError:
                    pass
                # Section count: count of ## headings (rough estimate)
                try:
                    txt = md.read_text(encoding="utf-8", errors="replace")
                    sections_count += txt.count("\n## ")
                except OSError:
                    pass
            drawer_count = len(drawers)
            size_str = (
                f"{total_bytes / 1024:.1f}KB"
                if total_bytes < 1024 * 1024
                else f"{total_bytes / (1024 * 1024):.1f}MB"
            )
            typer.echo(
                f"- {tbl.name}/   ({drawer_count} drawers / {sections_count} sections / {size_str})"
            )
    typer.echo("")

    # cue-related candidate drawers
    query_str = _p.compose_query(cues=cues, entities=entities, kind=kind_norm)
    candidates: list[dict[str, Any]] = []
    nav_warnings: list[str] = []
    if query_str:
        with _open(data_dir) as nav_db:
            try:
                nav_result = nav_db.navigate(
                    query_str,
                    max_routes=max_routes,
                    fallback_disabled=False,
                )
            except Exception as exc:  # noqa: BLE001
                nav_result = {"routes": [], "warnings": [f"navigate failed: {exc}"]}
        candidates = _p.section_routes_from_navigate(nav_result)
        nav_warnings = list(nav_result.get("warnings") or [])

    # drawer-level candidates (collapse section routes by drawer)
    drawer_seen: dict[str, dict[str, Any]] = {}
    for r in candidates:
        key = f"{r.get('table')}/{r.get('drawer')}"
        if key not in drawer_seen:
            drawer_seen[key] = r

    if drawer_seen:
        typer.echo("## cue-related candidate drawers (if any)")
        for idx, (dkey, r) in enumerate(list(drawer_seen.items())[:5], start=1):
            label = chr(ord("A") + idx - 1)
            dr_path = data_dir / str(r.get("table", "")) / f"{r.get('drawer', '')}.md"
            try:
                dr_size = dr_path.stat().st_size
            except OSError:
                dr_size = 0
            pct = dr_size / (50 * 1024 * 1024) * 100
            size_str = (
                f"{dr_size / 1024:.1f}KB"
                if dr_size < 1024 * 1024
                else f"{dr_size / (1024 * 1024):.1f}MB"
            )
            # Section count estimate (rough)
            try:
                txt = dr_path.read_text(encoding="utf-8", errors="replace")
                sec_cnt = txt.count("\n## ")
            except OSError:
                sec_cnt = 0
            # cue token index: signals or empty list
            signals = r.get("signals") or []
            cue_tokens = [str(s) for s in signals[:3]]
            typer.echo(f"[{label}] {dkey}")
            typer.echo(f"    {sec_cnt} sections / {size_str} / {pct:.1f}% of 50MB")
            if cue_tokens:
                typer.echo(f"    cue token index: {cue_tokens}")
        typer.echo("")

    # 1/2/3 options
    typer.echo("## where to put it?")
    typer.echo("(1) Append to existing drawer (--ref <table>/<drawer>#<section>) -- strongest recall, recommended")
    typer.echo("(2) New drawer in existing folder (--table <t> --new-drawer <slug>)")
    typer.echo("(3) New drawer in new folder (--new-table <t> --new-drawer <slug>)")
    typer.echo("")
    typer.echo("Auto-split is suggested as drawers approach 50MB.")

    for w in nav_warnings:
        typer.echo(f"warning: {w}", err=True)


def _write_call2_plan(
    data_dir: Path,
    *,
    dest_table: str,
    dest_drawer: str,
    dest_section: str,
    cues: list[str],
    entities: list[str],
    kind_norm: str | None,
) -> None:
    """Call 2: location decision + format guide (dry-run, no save)."""

    _SPLIT_THRESHOLD = 40 * 1024 * 1024  # 40MB -> split recommended
    _HARD_CAP = 50 * 1024 * 1024  # 50MB

    typer.echo("# mddbai write — call 2: location decision")
    typer.echo("")
    typer.echo("## destination")
    typer.echo(f"{dest_table}/{dest_drawer}#{dest_section}")
    typer.echo("")
    typer.echo("## location review (informational, does not refuse)")

    # drawer size
    dr_path = data_dir / dest_table / f"{dest_drawer}.md"
    try:
        dr_size = dr_path.stat().st_size
    except OSError:
        dr_size = 0

    pct = dr_size / _HARD_CAP * 100
    size_str = (
        f"{dr_size / 1024:.1f}KB"
        if dr_size < 1024 * 1024
        else f"{dr_size / (1024 * 1024):.1f}MB"
    )
    size_line = f"- drawer size: {size_str} / 50MB ({pct:.1f}% used)"
    if dr_size >= _SPLIT_THRESHOLD:
        size_line += " -- split_recommended"
    else:
        size_line += " -- plenty of room"
    typer.echo(size_line)

    # cue <-> slug token overlap (rough)
    from mddbai.cli._placement import slugify as _slugify  # noqa: PLC0415

    drawer_tokens = set(dest_drawer.replace("/", "-").split("-"))
    cue_tokens_all: list[str] = []
    for c in cues + entities:
        cue_tokens_all.extend(_slugify(c, max_words=4).split("-"))
    overlap = [t for t in cue_tokens_all if t and t in drawer_tokens]
    if overlap:
        typer.echo(f"- cue <-> slug token overlap: {overlap[:3]} ({len(overlap)} tokens)")
    else:
        typer.echo(
            "- cue <-> slug token overlap: 0 tokens -- next recall may have trouble entering via cue"
        )

    # kind/table alignment (rough)
    if kind_norm:
        expected_table = kind_norm if kind_norm.endswith("s") else kind_norm + "s"
        if dest_table == expected_table:
            typer.echo(f"- kind/table aligned: '{dest_table}' matches kind='{kind_norm}' axis OK")
        else:
            typer.echo(
                f"- kind/table aligned: kind='{kind_norm}' expected table '{expected_table}'"
                f" but actual '{dest_table}' -- informational only"
            )

    typer.echo("")
    typer.echo("## write format (for next recall)")
    typer.echo("- cue: time/place/person + key word (2~5 words recommended)")
    typer.echo("- body >= 1KB: split by ## H2 into semantic units (cold AI can take only the exact section)")
    typer.echo("- frontmatter cue token index (1~2 words) is auto-written; rich cues belong inside H2 body")
    typer.echo("")
    typer.echo("## next call (save)")
    kind_part = f" --kind {kind_norm}" if kind_norm else ""
    cue_part = "".join(f" --cue '{c}'" for c in cues)
    entity_part = "".join(f" --entity '{e}'" for e in entities)
    typer.echo(
        f"mddbai write <data_dir>{kind_part}{cue_part}{entity_part} \\\n"
        f"  --ref {dest_table}/{dest_drawer}#{dest_section} \\\n"
        f"  --body <text> --yes"
    )


@app.command("write")
def write_cmd(  # noqa: PLR0912, PLR0915
    data_dir: Path,
    body: str = typer.Option(
        "",
        "--body",
        help="Section body. Avoids the positional `-` prefix pitfall.",
    ),
    body_stdin: bool = typer.Option(
        False, "--body-stdin", help="Read body from stdin in full."
    ),
    body_file: Path | None = typer.Option(
        None, "--body-file", help="Read body from a file."
    ),
    cue: list[str] = typer.Option(
        [], "--cue", help="Recall cue (repeatable)."
    ),
    entity: list[str] = typer.Option(
        [], "--entity", help="Noun-form keyword (repeatable)."
    ),
    alias: list[str] = typer.Option(
        [],
        "--alias",
        help=(
            "Korean <-> English <-> abbreviation cue mapping (repeatable). "
            "Stored so the AI can reason across languages on the next recall. "
            "Example: --alias act-one --alias first-act --alias preparation"
        ),
    ),
    because: str = typer.Option(
        "",
        "--because",
        help=(
            "One-line explanation of why this destination was chosen (reasoning trace). "
            "The next cold AI receives it alongside the body on take/read and can "
            "continue the prior reasoning path. Example: --because 'Q4 deferral decision — supply chain + marketing'"
        ),
    ),
    kind: str = typer.Option(
        "",
        "--kind",
        help="write intent: memo|decision|rule|session|knowledge|source|todo|revision (custom slugs also allowed).",
    ),
    ref: str = typer.Option(
        "",
        "--ref",
        help="Explicit destination — '<table>/<drawer>#<section>'. Bypasses candidates / recommendations.",
    ),
    select_idx: int = typer.Option(
        0,
        "--select",
        help="Reuse existing candidate N (1-indexed). 0 = not selected.",
        min=0,
    ),
    table: str = typer.Option("", "--table", help="Explicit destination — table."),
    drawer: str = typer.Option("", "--drawer", help="Explicit destination — drawer."),
    section: str = typer.Option("", "--section", help="Explicit destination — section."),
    new_table: str = typer.Option(
        "", "--new-table", help="New folder (table) slug. Use with --new-drawer."
    ),
    new_drawer: str = typer.Option(
        "", "--new-drawer", help="Override recommendation — new drawer slug (may include table)."
    ),
    new_section: str = typer.Option(
        "", "--new-section", help="Override recommendation — new section_id."
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Non-interactive save. Destination must be explicit."
    ),
    skip_recall_check: bool = typer.Option(
        False,
        "--skip-recall-check",
        help="Skip self-test after save (default is to run it).",
    ),
    # Metadata helpers
    date: str = typer.Option("", "--date", help="ISO 8601 date."),
    source: str = typer.Option(
        "", "--source", help="ai|user|cite|tool|import."
    ),
    state: str = typer.Option(
        "", "--state", help="active|superseded|deprecated."
    ),
    confidence: float | None = typer.Option(
        None, "--confidence", min=0.0, max=1.0
    ),
    importance: float | None = typer.Option(
        None, "--importance", min=0.0, max=1.0
    ),
    related: list[str] = typer.Option(
        [], "--related", help="Adjacent ref (repeatable)."
    ),
    memory_zone: str = typer.Option("", "--memory-zone"),
    lang: str = typer.Option(
        "auto",
        "--lang",
        help=(
            "1-character language inference hint (lets cold AI cross-language reason on recall). "
            "'auto' (default) = auto-detect from body + cue (any Korean char -> 'ko', else 'en'). "
            "Explicit: 'ko' / 'en' / 'ja' / 'en-us' etc. "
            "'' (empty string) = do not store."
        ),
    ),
    max_routes: int = typer.Option(5, "--max-routes"),
) -> None:
    """Placement-assist write — 3-call flow (or 1-call auto with --yes).

    Call 1 (map, dry-run): no destination, no --yes -> print palace + folder list + cue candidate drawers.
    Call 2 (location): destination option but no --yes -> location review + format guide.
    Call 3 (save): --yes -> actual save + recall-check (weak/miss also saved, stderr warning only).

    Auto placement: --yes alone (no destination options) -> save at the recommended
    location derived from --kind / --cue / --entity. First-write friendly.

    All output is Markdown (R4 / D1 aligned).
    """

    from mddbai.cli import _placement as _p  # noqa: PLC0415
    from mddbai.brain import tutorial  # noqa: PLC0415

    # Record this call (front-door)
    tutorial.record_call(data_dir, cmd="write", via_door=True)

    body_sources = sum(
        [1 if body else 0, 1 if body_stdin else 0, 1 if body_file is not None else 0]
    )
    if body_sources > 1:
        typer.echo(
            "error: more than one body input path — choose only one of --body / --body-stdin / --body-file.",
            err=True,
        )
        raise typer.Exit(code=2)

    resolved_body: str | None = None
    if body_stdin:
        resolved_body = sys.stdin.read()
    elif body_file is not None:
        resolved_body = body_file.read_text(encoding="utf-8")
    elif body:
        resolved_body = body

    cues = [c for c in cue if c]
    entities = [e for e in entity if e]
    kind_norm = kind.strip() or None

    # ---- Partial destination check (preserves user-specified location with --yes) ----
    # User decision 2026-05-08 — when only part of the destination is specified,
    # block the trap where the missing part falls back to recommendation or to
    # the call-1 dump layout. Trust user-specified positions: never override
    # what the user wrote.
    if yes and not ref and select_idx == 0:
        valid_combo = (
            # (1) Full explicit — existing section
            (bool(table) and bool(drawer) and bool(section)
             and not (new_table or new_drawer))
            # (2) New table + new drawer (+ optional new_section)
            or (bool(new_table) and bool(new_drawer)
                and not (drawer or section))
            # (3) New drawer alone, or with explicit --table (+ optional new_section)
            or (bool(new_drawer) and not new_table
                and not (drawer or section))
            # (4) No destination options — intentional fallback to recommendation
            or not (
                table or drawer or section
                or new_table or new_drawer or new_section
            )
        )
        if not valid_combo:
            adapted: list[str] = []
            if table:
                adapted.append("--table")
            if drawer:
                adapted.append("--drawer")
            if section:
                adapted.append("--section")
            if new_table:
                adapted.append("--new-table")
            if new_drawer:
                adapted.append("--new-drawer")
            if new_section:
                adapted.append("--new-section")
            typer.echo(
                "error: partial destination — the user-specified options ("
                + ",".join(adapted)
                + ") are not enough to determine a location. "
                "Use one of:\n"
                "  (1) --table T --drawer D --section S         (existing section)\n"
                "  (2) --new-table T --new-drawer D [--new-section S]  (new table + drawer)\n"
                "  (3) --new-drawer D [--table T] [--new-section S]    (new drawer; optionally explicit table)\n"
                "  (4) no destination options — uses recommended location\n"
                "  (5) --ref T/D#S or --select N",
                err=True,
            )
            raise typer.Exit(code=2)

    # ---- Decide whether destination is given (branches calls 1/2/3) -----
    # Destination options: --ref, --select, --table+--drawer+--section, --new-table+--new-drawer, --new-drawer, --new-section
    has_destination = bool(
        ref
        or select_idx > 0
        or (table and drawer and section)
        or new_table
        or new_drawer
        or new_section
    )

    # ---- Call 1: dump map (no destination, dry-run) -------------------
    # `--yes` without a destination falls through to recommendation
    # (auto-placement) — first-write friendly. Without `--yes`, dump map.
    if not has_destination and not yes:
        _write_call1_map(
            data_dir,
            cues=cues,
            entities=entities,
            kind_norm=kind_norm,
            max_routes=max_routes,
        )
        return

    # ---- Decide destination (priority: --ref > --select > --table trio > --new-table+new-drawer > --new-* > recommendation)
    query_str = _p.compose_query(cues=cues, entities=entities, kind=kind_norm)
    candidates: list[dict[str, Any]] = []
    nav_warnings: list[str] = []
    if query_str and select_idx > 0:
        with _open(data_dir) as nav_db:
            try:
                nav_result = nav_db.navigate(
                    query_str,
                    max_routes=max_routes,
                    fallback_disabled=False,
                )
            except Exception as exc:  # noqa: BLE001
                nav_result = {"routes": [], "warnings": [f"navigate failed: {exc}"]}
        candidates = _p.section_routes_from_navigate(nav_result)
        nav_warnings = list(nav_result.get("warnings") or [])

    recommendation = _p.recommend_new_placement(
        cues=cues,
        entities=entities,
        kind=kind_norm,
        body_preview=resolved_body[:200] if resolved_body else None,
    )

    dest_table: str | None = None
    dest_drawer: str | None = None
    dest_section: str | None = None
    dest_origin = "recommended"

    if ref:
        try:
            dest_table, dest_drawer, dest_section = _p.parse_ref(ref)
            dest_origin = "ref"
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
    elif select_idx > 0:
        if select_idx > len(candidates):
            typer.echo(
                f"error: --select {select_idx} out of range ({len(candidates)} candidates).",
                err=True,
            )
            raise typer.Exit(code=2)
        cand = candidates[select_idx - 1]
        dest_table = str(cand["table"])
        dest_drawer = str(cand["drawer"])
        dest_section = str(cand["section"])
        dest_origin = "select"
    elif table and drawer and section:
        dest_table = table
        dest_drawer = drawer
        dest_section = section
        dest_origin = "explicit"
    elif new_table and new_drawer:
        dest_table = new_table
        dest_drawer = new_drawer
        dest_section = new_section or recommendation.section
        dest_origin = "new-table-drawer"
    elif new_drawer:
        if "/" in new_drawer:
            t, d = new_drawer.split("/", 1)
            dest_table = t
            dest_drawer = d
        else:
            # User-specified --table wins. Trust the user-specified position —
            # never substitute it with a recommendation.
            dest_table = table if table else recommendation.table
            dest_drawer = new_drawer
        dest_section = new_section or recommendation.section
        dest_origin = "new-override"
    elif new_section:
        dest_table = recommendation.table
        dest_drawer = recommendation.drawer
        dest_section = new_section
        dest_origin = "new-section"
    else:
        dest_table = recommendation.table
        dest_drawer = recommendation.drawer
        dest_section = recommendation.section
        dest_origin = "recommended"

    assert dest_table is not None
    assert dest_drawer is not None
    assert dest_section is not None

    # ---- Call 2: location decision + format guide (no --yes) ----------
    if not yes:
        _write_call2_plan(
            data_dir,
            dest_table=dest_table,
            dest_drawer=dest_drawer,
            dest_section=dest_section,
            cues=cues,
            entities=entities,
            kind_norm=kind_norm,
        )
        for w in nav_warnings:
            typer.echo(f"warning: {w}", err=True)
        return

    # ---- Call 3: actual save (--yes given) -----------------------------
    if resolved_body is None:
        typer.echo(
            "error: with --yes, one of --body / --body-stdin / --body-file is required.",
            err=True,
        )
        raise typer.Exit(code=2)

    with _open(data_dir) as db:
        db.put_section(dest_table, dest_drawer, dest_section, resolved_body, fsync=True)

        cue_arg = list(cues) if cues else None
        entity_arg = list(entities) if entities else None
        related_arg = list(related) if related else None
        alias_arg = list(alias) if alias else None
        because_arg = because.strip() or None
        date_arg = date or None
        source_arg = source or None
        state_arg = state or None
        zone_arg = memory_zone or None

        # Language inference hint — 'auto' detects from body + cue, '' = do not store,
        # otherwise pass through (validated at parse time).
        from mddbai.codec.section_meta import detect_lang as _detect_lang  # noqa: PLC0415

        lang_norm = lang.strip().lower()
        lang_arg: str | None
        if lang_norm == "":
            lang_arg = None
        elif lang_norm == "auto":
            lang_arg = _detect_lang(
                resolved_body or "",
                *(cues or []),
                *(entities or []),
                dest_section or "",
            )
        else:
            lang_arg = lang_norm

        if (
            cue_arg is not None
            or entity_arg is not None
            or related_arg is not None
            or alias_arg is not None
            or because_arg is not None
            or date_arg is not None
            or source_arg is not None
            or state_arg is not None
            or zone_arg is not None
            or confidence is not None
            or importance is not None
            or lang_arg is not None
        ):
            meta_kwargs: dict[str, Any] = {
                "cue": cue_arg,
                "entity": entity_arg,
                "related": related_arg,
                "date": date_arg,
                "source": source_arg,
                "state": state_arg,
                "memory_zone": zone_arg,
                "confidence": confidence,
                "importance": importance,
                "merge": True,
            }
            if alias_arg is not None:
                meta_kwargs["aliases"] = alias_arg
            if because_arg is not None:
                meta_kwargs["chosen_because"] = because_arg
            if lang_arg is not None:
                meta_kwargs["lang"] = lang_arg
            db.put_section_meta(
                dest_table,
                dest_drawer,
                dest_section,
                **meta_kwargs,
            )

        # Auto-preserve structural cue (only when no explicit cue — D2 aligned).
        if cue_arg is None:
            existing = db.get_section_meta(dest_table, dest_drawer, dest_section) or {}
            if not existing.get("cue"):
                from mddbai.codec.section_meta import (  # noqa: PLC0415
                    derive_structural_cue,
                )

                preview = resolved_body[:80] if resolved_body else ""
                derived = derive_structural_cue(
                    section_id=dest_section,
                    drawer_rel=dest_drawer,
                    heading=dest_section,
                    body_preview=preview,
                )
                if derived:
                    db.put_section_meta(
                        dest_table,
                        dest_drawer,
                        dest_section,
                        cue=derived,
                        merge=True,
                    )

        # related links (bidirectional) — never automatic. Only explicit --related is written.

        # Self-test after flush (recall-check) — weak/miss also saved, stderr warning only.
        db.flush()

        # Record drawer size (for output)
        dr_path = data_dir / dest_table / f"{dest_drawer}.md"
        try:
            dr_size_after = dr_path.stat().st_size
        except OSError:
            dr_size_after = 0

        recall_status = "skipped"
        recall_msg: str | None = None
        if not skip_recall_check:
            self_cue = " ".join(
                (cue_arg or []) + (entity_arg or []) + [dest_section]
            )[:100]
            if self_cue.strip():
                try:
                    nav = db.navigate(
                        self_cue, max_routes=5, fallback_disabled=True
                    )
                    section_routes = _p.section_routes_from_navigate(nav)
                    hit = any(
                        r["table"] == dest_table
                        and r["drawer"] == dest_drawer
                        and r["section"] == dest_section
                        for r in section_routes
                    )
                    if hit:
                        recall_status = "ok"
                    else:
                        # weak/miss — already saved, stderr warning only (rc 0)
                        cand_count = len(section_routes)
                        if cand_count > 0:
                            recall_status = "weak"
                            cands = [
                                f"{r['table']}/{r['drawer']}#{r['section']}"
                                for r in section_routes[:3]
                            ]
                            recall_msg = (
                                f"[recall-check weak] self cue '{self_cue.strip()}' "
                                f"got {cand_count} candidates but self not top. "
                                f"closest: {', '.join(cands)}. "
                                "Add more --cue / --entity tokens for stronger recall."
                            )
                        else:
                            recall_status = "miss"
                            recall_msg = (
                                f"[recall-check miss] self cue '{self_cue.strip()}' "
                                "returned 0 candidates. "
                                "Add more --cue / --entity tokens for next session recall."
                            )
                except Exception as exc:  # noqa: BLE001
                    recall_status = "skipped"
                    recall_msg = f"navigate failed: {exc}"

    # split_recommended signal
    _SPLIT_THRESHOLD = 40 * 1024 * 1024
    split_signal = ""
    if dr_size_after >= _SPLIT_THRESHOLD:
        split_signal = f" -- split_recommended (>= 40MB)"

    size_str = (
        f"{dr_size_after / 1024:.1f}KB"
        if dr_size_after < 1024 * 1024
        else f"{dr_size_after / (1024 * 1024):.1f}MB"
    )
    pct = dr_size_after / (50 * 1024 * 1024) * 100

    # Mark graduation
    tutorial.advance_step(data_dir, next_step=tutorial.Step.GRADUATED)

    typer.echo(f"## Saved: {dest_table}/{dest_drawer}#{dest_section}")
    typer.echo(f"origin: {dest_origin}")
    typer.echo(f"drawer: {size_str} / 50MB ({pct:.1f}%){split_signal}")
    typer.echo(f"recall-check: {recall_status}")
    if recall_msg:
        typer.echo(f"# {recall_msg}", err=True)
    for w in nav_warnings:
        typer.echo(f"warning: {w}", err=True)


@app.command("read")
def read_cmd(  # noqa: PLR0912
    data_dir: Path,
    query: str = typer.Argument(..., help="One-line natural-language cue."),
    select_idx: int = typer.Option(
        0,
        "--select",
        help="Force-select candidate N (1-indexed). 0 = automatic (uses the section if 1 candidate).",
        min=0,
    ),
    ref: str = typer.Option(
        "",
        "--ref",
        help="Explicit ref '<table>/<drawer>#<section>' — bypasses navigate.",
    ),
    max_routes: int = typer.Option(5, "--max-routes"),
    include_superseded: bool = typer.Option(
        False, "--include-superseded", help="Include superseded/deprecated candidates."
    ),
) -> None:
    """Placement-assist read — 1 candidate prints body, N candidates prints Markdown list only.

    Zero whole-drawer reads. Candidates are composed only from cue / section_id /
    sections_meta / summary / related (D8 aligned). With N candidates the body
    is 0 bytes; narrow with --select N or --ref. With 0 candidates, suggests
    augmentation across 4 dimensions (time/place/person/sense) in Markdown.
    """

    from mddbai.cli import _placement as _p  # noqa: PLC0415
    from mddbai.brain import tutorial  # noqa: PLC0415

    # Record this call (front-door)
    tutorial.record_call(data_dir, cmd="read", via_door=True)

    # If --ref is given, bypass navigate (semantic decision is up to the caller).
    if ref:
        try:
            t, d, s = _p.parse_ref(ref)
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        with _open(data_dir) as db:
            body = db.take_section(t, d, s, body_only=False)
        if body is None:
            typer.echo(f"error: ref not found: {t}/{d}#{s}", err=True)
            raise typer.Exit(code=1)
        typer.echo("# mddbai read")
        typer.echo("")
        typer.echo(f"Source: {t}/{d}#{s}")
        typer.echo("")
        typer.echo(body)
        return

    with _open(data_dir) as db:
        nav = db.navigate(
            query,
            max_routes=max_routes,
            fallback_disabled=True,
        )

        section_routes = _p.section_routes_from_navigate(nav)

        # state filter.
        if not include_superseded:
            kept: list[dict[str, Any]] = []
            for r in section_routes:
                try:
                    meta = db.get_section_meta(
                        str(r["table"]), str(r["drawer"]), str(r["section"])
                    ) or {}
                except Exception:  # noqa: BLE001
                    meta = {}
                st = meta.get("state")
                if st in (None, "active"):
                    kept.append(r)
            section_routes = kept

        warnings = list(nav.get("warnings") or [])

        # --- 0 candidates — 4-dimension augmentation hint (T.5) -------
        if not section_routes:
            typer.echo("# mddbai read")
            typer.echo("")
            typer.echo(f"query: {query}")
            typer.echo("")
            typer.echo("## Not found")
            typer.echo("No section-level route. (drawer body 0 byte read.)")
            typer.echo("")
            typer.echo("## Suggested boost — which of the 4 cue dimensions can you add?")
            typer.echo("")
            typer.echo("| dimension | example | how to add |")
            typer.echo("|---|---|---|")
            typer.echo("| time (when?) | '2026-05-07', 'last Tuesday', '3 weeks ago' | add date/period words |")
            typer.echo("| place (where?) | 'meeting room', 'Seoul', 'online' | add place words |")
            typer.echo("| person (who?) | 'Alice', 'teammate', 'customer' | add name/role words |")
            typer.echo("| sense (sense?) | 'red', 'loud', 'cold' | add sense/color words |")
            typer.echo("")
            typer.echo("Additional hints:")
            typer.echo("- Add noun-form keywords (the words written via --entity at write time)")
            typer.echo("- `mddbai navigate <data_dir> \"<cue>\"` shows drawer/table-level cues")
            typer.echo("- `mddbai cues <data_dir>` dumps every stored cue trace")
            typer.echo("")
            typer.echo("## reasoning cycle (Korean cue <-> English slug mapping) — no escape")
            typer.echo("")
            typer.echo("Core of mddbai = the AI reasons directly. Don't escape via Grep / whole-file Read.")
            typer.echo("")
            typer.echo("[Step 1] *break down the meaning* of a Korean cue")
            typer.echo("  e.g. 'act 1' -> first act = preparation = beginning")
            typer.echo("[Step 2] generate English synonyms (your own vocabulary)")
            typer.echo("  e.g. ['first act', 'preparation', 'phase 1', 'act 1', 'intro']")
            typer.echo("[Step 3] use `mddbai list-sections <data> <table> <drawer>`")
            typer.echo("  to find *common tokens* with section slugs:")
            typer.echo("  - 'PreparationPhase' -> matches 'preparation'")
            typer.echo("  - 'Phase1'           -> matches 'phase 1'")
            typer.echo("[Step 4] `mddbai take <data> <table> <drawer> <picked-section>`")
            typer.echo("")
            typer.echo("Write aliases at write time (stronger next-recall):")
            typer.echo("  mddbai write <data> --kind <k> --cue '<cue>' \\\\ ")
            typer.echo("    --alias act-one --alias first-act --alias preparation")
            typer.echo("  -> frontmatter `aliases: [act-one, first-act, preparation]` is stored")
            typer.echo("  -> next cold AI can recall via Korean or English")
            typer.echo("")
            typer.echo("Skill: .claude/skills/mddbai-recall/SKILL.md (4-step reasoning template)")
            for w in warnings:
                typer.echo(f"warning: {w}", err=True)
            raise typer.Exit(code=1)

        # --- Candidate selection ---------------------------------------
        chosen: dict[str, Any] | None = None
        if select_idx > 0:
            if select_idx > len(section_routes):
                typer.echo(
                    f"error: --select {select_idx} out of range ({len(section_routes)} candidates).",
                    err=True,
                )
                raise typer.Exit(code=2)
            chosen = section_routes[select_idx - 1]
        elif len(section_routes) == 1:
            chosen = section_routes[0]

        # --- N candidates, no selection — 0-byte body ------------------
        if chosen is None:
            typer.echo("# mddbai read")
            typer.echo("")
            typer.echo(f"query: {query}")
            typer.echo("")
            typer.echo(f"## Ambiguous — {len(section_routes)} candidates")
            for line in _p.render_candidates_md(
                section_routes, title="### Candidates"
            ):
                typer.echo(line)
            typer.echo("")
            typer.echo("Pick one with --select N or --ref <table>/<drawer>#<section>.")
            for w in warnings:
                typer.echo(f"warning: {w}", err=True)
            raise typer.Exit(code=1)

        # --- Exact 1 section body --------------------------------------
        body = db.take_section(
            str(chosen["table"]),
            str(chosen["drawer"]),
            str(chosen["section"]),
            body_only=False,
        )

    if body is None:
        typer.echo(
            f"error: candidate disappeared — {chosen.get('table')}/"
            f"{chosen.get('drawer')}#{chosen.get('section')}",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo("# mddbai read")
    typer.echo("")
    typer.echo(f"Source: {chosen['table']}/{chosen['drawer']}#{chosen['section']}")
    typer.echo("")
    typer.echo(body)
    for w in warnings:
        typer.echo(f"warning: {w}", err=True)


def main() -> None:  # pragma: no cover - called by typer
    # Stage N.5.1 — avoid Windows cp949 encoding crashes.
    # Cases where Korean / Unicode characters in `mddbai --help` (e.g. em dash
    # `—`) trigger UnicodeEncodeError on cp949 stdout (Gap 5).
    # Force this at CLI entry point rather than relying on PYTHONIOENCODING.
    import sys as _sys  # noqa: PLC0415

    for stream in (_sys.stdout, _sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass  # Some environments (e.g. pytest capture) don't support reconfigure
    app()


if __name__ == "__main__":  # pragma: no cover
    main()

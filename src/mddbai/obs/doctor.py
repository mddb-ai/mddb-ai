from __future__ import annotations

"""``mddbai doctor`` — drawer-native self-diagnostic.

Stage X (2026-05-06): rewritten as drawer-only after retiring the
record/schema/layout layers.

Checks:
- drawer_size: drawer .md exceeds size threshold (D2 aligned — warn only,
  no automatic split)
- duplicate_drawer: same semantic slug appears in multiple folders
- unstable_naming: temporary slugs like ``t<HHMMSS>-s<id>`` / ``s<id>``
  remain on disk
- weak_section_naming: H2 section titles too short or weak in meaning
- orphan_summary: ``_summary.md`` placed in a folder with no drawers
- broken_related: ``related`` frontmatter points at a missing drawer
- branch_overflow: folder has too many children (over threshold)
- english_naming: non-english identifier
- homeostasis_state: last sleep cycle reconcile activity
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from mddbai.brain.drawer_summary import SUMMARY_NAME
from mddbai.codec.frontmatter import parse as fm_parse
from mddbai.codec.section_meta import parse_sections_meta
from mddbai.core.errors import CodecError
from mddbai.core.types import TableName

if TYPE_CHECKING:
    from mddbai.engine import Database


SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_ERROR = "error"

# Temporary slug patterns (real-usage.md): the Stop hook writes
# ``t<HHMMSS>-s<sid>`` / ``s<sid>`` (sid 6~8 chars). If the AI never
# replaces these with semantic slugs they remain — a leftover signal.
_UNSTABLE_DRAWER_PATTERN = re.compile(r"^t\d{6}-s[0-9a-f]{4,8}$")
_UNSTABLE_SECTION_PATTERN = re.compile(r"^s[0-9a-f]{6,12}$")
# Weak slug — length < 3, digits only, or single ASCII letter.
_WEAK_SLUG_PATTERN = re.compile(r"^(\d+|[a-z])$")

# System prefixes excluded from inspection.
_SYSTEM_DIR_NAMES = frozenset({
    "_palace",
    "_drawers",
    "_user_lexicon",
    "_inbox",
    "_clusters",
    "_homeostasis",
    "_wal",
    "_registry",
})


@dataclass(frozen=True, slots=True)
class DoctorIssue:
    code: str
    severity: str
    target: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code} {self.target} — {self.message}"


@dataclass
class DoctorReport:
    data_dir: str
    tables: list[str] = field(default_factory=list)
    issues: list[DoctorIssue] = field(default_factory=list)
    info: list[DoctorIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[DoctorIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[DoctorIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_WARN]

    def is_healthy(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        lines = [f"data_dir: {self.data_dir}"]
        lines.append(f"tables: {', '.join(self.tables) or '(none)'}")
        lines.append(
            f"issues: {len(self.errors)} error / {len(self.warnings)} warn / {len(self.info)} info"
        )
        for issue in [*self.issues, *self.info]:
            lines.append(f"  {issue}")
        if self.errors:
            lines.append(
                f"\nverdict: needs_attention. ({len(self.errors)} errors)"
            )
        elif self.warnings:
            lines.append(
                f"\nverdict: ok_with_warnings. ({len(self.warnings)} warnings)"
            )
        else:
            lines.append("\nverdict: healthy. (no issues)")
        return "\n".join(lines)


def check(db: Database) -> DoctorReport:
    """Run every check at once. drawer-native (zero dependency on the record model)."""

    report = DoctorReport(data_dir=str(db.config.data_dir))
    try:
        tables = db.tables()
    except Exception:  # noqa: BLE001
        tables = []
    report.tables = [str(t) for t in tables]

    for table_name in sorted(set(report.tables)):
        table = TableName(table_name)
        _safe_check(report, "drawer_size", _check_drawer_size, db, table)
        _safe_check(report, "navigation_contract", _check_navigation_contract, db, table)
        _safe_check(report, "duplicate_drawer", _check_duplicate_drawer, db, table)
        _safe_check(report, "unstable_naming", _check_unstable_naming, db, table)
        _safe_check(report, "weak_section_naming", _check_weak_section_naming, db, table)
        _safe_check(report, "orphan_summary", _check_orphan_summary, db, table)
        _safe_check(report, "broken_related", _check_broken_related, db, table)

    _safe_check(report, "branch_overflow", _check_branch_overflow, db)
    _safe_check(report, "homeostasis_state", _check_homeostasis_state, db)
    _safe_check(report, "english_naming", _check_english_naming, db)
    _safe_check(report, "navigation_root", _check_navigation_root, db)
    return report


def _safe_check(report: DoctorReport, label: str, fn, *args) -> None:  # type: ignore[no-untyped-def]
    """Invoke a diagnostic function, downgrading any exception to an issue."""

    try:
        fn(*args, report)
    except Exception as exc:  # noqa: BLE001
        path_hint = _extract_path_hint(exc)
        target = f"{label} @ {path_hint}" if path_hint else label
        report.issues.append(
            DoctorIssue(
                code="diagnostic_failed",
                severity=SEVERITY_WARN,
                target=target,
                message=f"{type(exc).__name__}: {exc}",
            )
        )


def _extract_path_hint(exc: BaseException, depth: int = 0) -> str | None:
    if depth > 4:
        return None
    fn = getattr(exc, "filename", None)
    if isinstance(fn, str) and fn:
        return fn
    ctx = getattr(exc, "context", None)
    if isinstance(ctx, dict):
        for key in ("path", "file", "filename", "target"):
            v = ctx.get(key)
            if isinstance(v, str) and v:
                return v
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        return _extract_path_hint(cause, depth + 1)
    return None


# ---- iteration helpers ---------------------------------------------------


def _iter_drawer_files(table_root: Path) -> list[Path]:
    """Drawer ``.md`` files inside the table. System-prefixed dirs and sidecars are skipped."""

    if not table_root.exists():
        return []
    out: list[Path] = []
    for path in table_root.rglob("*.md"):
        if path.is_dir():
            continue
        rel_parts = path.relative_to(table_root).parts
        if any(part in _SYSTEM_DIR_NAMES for part in rel_parts):
            continue
        if any(part.startswith("_") for part in rel_parts):
            continue
        if path.name.endswith(".lock") or path.name.endswith(".tmp") or path.name.endswith(".bak"):
            continue
        out.append(path)
    return out


def _drawer_rel(table_root: Path, path: Path) -> str:
    """Return the ``<sub>/<drawer>`` relative path (``.md`` stripped)."""

    rel = path.relative_to(table_root).as_posix()
    return rel[:-3] if rel.endswith(".md") else rel


# ---- individual checks ---------------------------------------------------


_NAVIGATION_HARD_CAP_BYTES = 50 * 1024 * 1024


def _check_drawer_size(
    db: Database, table: TableName, report: DoctorReport
) -> None:
    """Warn only when a drawer .md exceeds the configured threshold (D2 aligned, no auto-split).

    In strict navigation mode:
    - over the configured threshold (drawer_split_bytes, default 256KB) -> ERROR (split recommended)
    - over the 1 MB hard cap -> ERROR (the navigation first-read would explode;
      even reading drawer frontmatter alone becomes too much cat overhead).
    """

    base = db.config.data_dir / str(table)
    threshold = getattr(db.config, "drawer_split_bytes", 256 * 1024)
    strict = bool(getattr(db.config, "navigation_strict", False))
    for path in _iter_drawer_files(base):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > threshold:
            rel = _drawer_rel(base, path)
            severity = SEVERITY_ERROR if strict else SEVERITY_WARN
            report.issues.append(
                DoctorIssue(
                    code="drawer_oversized",
                    severity=severity,
                    target=f"{table}/{rel}",
                    message=(
                        f"drawer {size:,} bytes > {threshold:,} "
                        f"(config.drawer_split_bytes); "
                        f"consider `mddbai split-drawer {db.config.data_dir} {table} {rel} --by time`"
                    ),
                )
            )
        if strict and size > _NAVIGATION_HARD_CAP_BYTES:
            rel = _drawer_rel(base, path)
            report.issues.append(
                DoctorIssue(
                    code="drawer_navigation_hard_cap",
                    severity=SEVERITY_ERROR,
                    target=f"{table}/{rel}",
                    message=(
                        f"drawer {size:,} bytes exceeds 50 MiB navigation hard "
                        "cap; navigation-first reads must stay light"
                    ),
                )
            )


def _check_navigation_root(db: Database, report: DoctorReport) -> None:
    """Strict navigation mode: root palace must exist."""

    if not getattr(db.config, "navigation_strict", False):
        return
    palace = db.config.data_dir / "_palace.md"
    if not palace.exists():
        report.issues.append(
            DoctorIssue(
                code="missing_navigation_root",
                severity=SEVERITY_ERROR,
                target="_palace.md",
                message=(
                    "navigation_strict requires root _palace.md so an AI can "
                    "choose the first route before reading table contents"
                ),
            )
        )


def _check_navigation_contract(
    db: Database, table: TableName, report: DoctorReport
) -> None:
    """Strict navigation mode: table, folder, drawer, and section cues must exist."""

    if not getattr(db.config, "navigation_strict", False):
        return

    base = db.config.data_dir / str(table)
    if not base.exists():
        return

    emitted = 0
    max_issues = 200

    def emit(code: str, target: str, message: str) -> None:
        nonlocal emitted
        if emitted >= max_issues:
            if emitted == max_issues:
                report.issues.append(
                    DoctorIssue(
                        code="navigation_issue_limit",
                        severity=SEVERITY_ERROR,
                        target=str(table),
                        message=(
                            f"navigation_strict found more than {max_issues} "
                            "issues; fix earlier issues and rerun doctor"
                        ),
                    )
                )
                emitted += 1
            return
        report.issues.append(
            DoctorIssue(
                code=code,
                severity=SEVERITY_ERROR,
                target=target,
                message=message,
            )
        )
        emitted += 1

    index_path = base / "_palace" / "INDEX.md"
    if not index_path.exists():
        emit(
            "missing_table_palace_index",
            f"{table}/_palace/INDEX.md",
            "navigation_strict requires a table map with purpose, routing rules, and responsibilities",
        )

    root_summary = base / SUMMARY_NAME
    if not root_summary.exists():
        emit(
            "missing_folder_summary",
            f"{table}/{SUMMARY_NAME}",
            "navigation_strict requires a table-root _summary.md signpost",
        )

    for folder in _iter_folders_with_drawers(base):
        summary_path = folder / SUMMARY_NAME
        if not summary_path.exists():
            try:
                rel = folder.relative_to(db.config.data_dir).as_posix()
            except ValueError:
                rel = str(folder)
            emit(
                "missing_folder_summary",
                f"{rel}/{SUMMARY_NAME}",
                "folder contains drawers but has no _summary.md navigation signpost",
            )

    required_drawer_meta = ("title", "summary", "cue", "tags")
    for path in _iter_drawer_files(base):
        rel = _drawer_rel(base, path)
        try:
            text = path.read_text(encoding="utf-8")
            meta, body = fm_parse(text)
        except (OSError, CodecError, ValueError) as exc:
            emit(
                "invalid_drawer_frontmatter",
                f"{table}/{rel}",
                f"drawer frontmatter must be readable for navigation: {exc}",
            )
            continue

        for key in required_drawer_meta:
            if not _has_navigation_value(meta.get(key)):
                emit(
                    "missing_drawer_navigation_meta",
                    f"{table}/{rel}",
                    f"drawer frontmatter missing non-empty {key!r}",
                )

        try:
            sections_meta = parse_sections_meta(meta)
        except Exception as exc:  # noqa: BLE001
            emit(
                "invalid_sections_meta",
                f"{table}/{rel}",
                f"sections_meta must parse: {type(exc).__name__}: {exc}",
            )
            sections_meta = {}

        for sid in _section_ids_from_body(body):
            sm = sections_meta.get(sid)
            if sm is None:
                emit(
                    "missing_section_navigation_meta",
                    f"{table}/{rel}#{sid}",
                    "section missing sections_meta entry",
                )
                continue
            if not sm.cue:
                emit(
                    "missing_section_cue",
                    f"{table}/{rel}#{sid}",
                    "section metadata must include non-empty cue",
                )
            if sm.importance is None:
                emit(
                    "missing_section_importance",
                    f"{table}/{rel}#{sid}",
                    "section metadata must include importance for routing priority",
                )
            if sm.memory_zone is None:
                emit(
                    "missing_section_memory_zone",
                    f"{table}/{rel}#{sid}",
                    "section metadata must include memory_zone for traversal policy",
                )


def _iter_folders_with_drawers(table_root: Path) -> list[Path]:
    folders: set[Path] = set()
    if not table_root.exists():
        return []
    for drawer in _iter_drawer_files(table_root):
        cur = drawer.parent
        while True:
            try:
                rel_parts = cur.relative_to(table_root).parts
            except ValueError:
                break
            if not any(part in _SYSTEM_DIR_NAMES or part.startswith("_") for part in rel_parts):
                folders.add(cur)
            if cur == table_root:
                break
            cur = cur.parent
    return sorted(folders)


def _has_navigation_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_has_navigation_value(v) for v in value)
    if isinstance(value, dict):
        return bool(value)
    return True


def _section_ids_from_body(body: str) -> list[str]:
    out: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## ") and not stripped.startswith("### "):
            sid = stripped[3:].strip()
            if sid:
                out.append(sid)
    return out


def _check_duplicate_drawer(
    db: Database, table: TableName, report: DoctorReport
) -> None:
    """Detect the same semantic slug placed under multiple parent folders.

    Example: ``code/2026-05-05/auth-fix.md`` and ``code/auth-fix.md`` coexisting
    -> the AI cannot tell which section to use and both grow (classic drift).
    """

    base = db.config.data_dir / str(table)
    by_stem: dict[str, list[str]] = {}
    for path in _iter_drawer_files(base):
        stem = path.stem
        if not stem:
            continue
        rel = _drawer_rel(base, path)
        by_stem.setdefault(stem, []).append(rel)
    for stem, rels in by_stem.items():
        if len(rels) >= 2:
            report.issues.append(
                DoctorIssue(
                    code="duplicate_drawer",
                    severity=SEVERITY_WARN,
                    target=f"{table}/{stem}",
                    message=(
                        f"drawer name {stem!r} found in {len(rels)} locations: "
                        f"{', '.join(sorted(rels))} — consolidate or rename"
                    ),
                )
            )


def _check_unstable_naming(
    db: Database, table: TableName, report: DoctorReport
) -> None:
    """Detect leftover temporary slugs (``t<HHMMSS>-s<id>`` / ``s<id>``).

    real-usage.md: temporary names written by the Stop hook must be replaced
    with semantic slugs by the AI at the end of every response. If they are
    still on disk, recall cues cannot be identified.
    """

    base = db.config.data_dir / str(table)
    for path in _iter_drawer_files(base):
        stem = path.stem
        if _UNSTABLE_DRAWER_PATTERN.match(stem):
            rel = _drawer_rel(base, path)
            report.issues.append(
                DoctorIssue(
                    code="unstable_drawer_name",
                    severity=SEVERITY_WARN,
                    target=f"{table}/{rel}",
                    message=(
                        f"drawer stem {stem!r} looks like Stop-hook temporary slug; "
                        f"rename via `mddbai rename-drawer {db.config.data_dir} {table} {rel} <semantic-slug>`"
                    ),
                )
            )
            continue
        # Also inspect H2 section IDs inside the same drawer.
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("## "):
                continue
            sid = stripped[3:].strip()
            if _UNSTABLE_SECTION_PATTERN.match(sid):
                rel = _drawer_rel(base, path)
                report.issues.append(
                    DoctorIssue(
                        code="unstable_section_name",
                        severity=SEVERITY_WARN,
                        target=f"{table}/{rel}#{sid}",
                        message=(
                            f"section {sid!r} looks like temporary slug; "
                            f"rename via `mddbai rename-section`"
                        ),
                    )
                )


def _check_weak_section_naming(
    db: Database, table: TableName, report: DoctorReport
) -> None:
    """Detect H2 slugs weak in meaning (length 1~2 or digits only)."""

    base = db.config.data_dir / str(table)
    for path in _iter_drawer_files(base):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("## "):
                continue
            sid = stripped[3:].strip()
            if _WEAK_SLUG_PATTERN.match(sid):
                rel = _drawer_rel(base, path)
                report.issues.append(
                    DoctorIssue(
                        code="weak_section_name",
                        severity=SEVERITY_INFO,
                        target=f"{table}/{rel}#{sid}",
                        message=(
                            f"section {sid!r} is too short to serve as a recall cue"
                        ),
                    )
                )


def _check_orphan_summary(
    db: Database, table: TableName, report: DoctorReport
) -> None:
    """Find ``_summary.md`` placed in a folder without a single drawer."""

    base = db.config.data_dir / str(table)
    if not base.exists():
        return
    for summary_path in base.rglob(SUMMARY_NAME):
        folder = summary_path.parent
        # Whether the folder (or any subtree) holds at least one drawer .md
        has_drawer = False
        for cand in folder.rglob("*.md"):
            if cand.name == SUMMARY_NAME:
                continue
            rel_parts = cand.relative_to(folder).parts
            if any(part in _SYSTEM_DIR_NAMES for part in rel_parts):
                continue
            if any(part.startswith("_") for part in rel_parts):
                continue
            if cand.name.endswith(".lock") or cand.name.endswith(".tmp"):
                continue
            has_drawer = True
            break
        if not has_drawer:
            try:
                rel = summary_path.relative_to(db.config.data_dir).as_posix()
            except ValueError:
                rel = str(summary_path)
            report.info.append(
                DoctorIssue(
                    code="orphan_summary",
                    severity=SEVERITY_INFO,
                    target=rel,
                    message=f"{SUMMARY_NAME} sits in a folder with no drawers",
                )
            )


def _check_broken_related(
    db: Database, table: TableName, report: DoctorReport
) -> None:
    """Verify ``related: [<table>/<drawer>, ...]`` in drawer frontmatter actually exists."""

    base = db.config.data_dir / str(table)
    if not base.exists():
        return
    data_dir = db.config.data_dir
    for path in _iter_drawer_files(base):
        try:
            text = path.read_text(encoding="utf-8")
            meta, _ = fm_parse(text)
        except (OSError, CodecError, ValueError):
            continue
        related = meta.get("related")
        if not isinstance(related, list):
            continue
        rel_self = _drawer_rel(base, path)
        for ref in related:
            if not isinstance(ref, str) or not ref.strip():
                continue
            ref = ref.strip()
            target_path = _resolve_related(data_dir, str(table), rel_self, ref)
            if target_path is None or not target_path.exists():
                report.issues.append(
                    DoctorIssue(
                        code="broken_related",
                        severity=SEVERITY_WARN,
                        target=f"{table}/{rel_self}",
                        message=(
                            f"related target {ref!r} does not resolve to an existing drawer"
                        ),
                    )
                )


def _resolve_related(
    data_dir: Path, table: str, rel_self: str, ref: str
) -> Path | None:
    """Resolve a ``related`` value to a file path.

    Accepted forms:
    - ``<table>/<drawer>`` (absolute — table name prefixed)
    - ``<drawer>`` (relative inside the same table)
    - ``./<drawer>`` or ``../<drawer>`` style relative paths
    """

    ref = ref.strip().lstrip("/")
    if not ref:
        return None
    if ref.endswith(".md"):
        ref = ref[:-3]
    # Absolute — table-prefixed form
    if "/" in ref:
        head = ref.split("/", 1)[0]
        if (data_dir / head).is_dir():
            return data_dir / f"{ref}.md"
    return data_dir / table / f"{ref}.md"


def _check_branch_overflow(db: Database, report: DoctorReport) -> None:
    """Branch overflow check — delegated to the branch_overflow module."""

    from mddbai.brain.branch_overflow import detect_overflow  # noqa: PLC0415

    reports = detect_overflow(db)
    for r in reports:
        report.issues.append(
            DoctorIssue(
                code="branch_overflow",
                severity=SEVERITY_WARN,
                target=r.rel_path,
                message=(
                    f"{r.child_count} children > threshold {r.threshold} — "
                    "see _attention.md and consider mddbai split-folder"
                ),
            )
        )


def _check_homeostasis_state(db: Database, report: DoctorReport) -> None:
    """Surface the last homeostasis counts from sleep state (avoid silent reconcile)."""

    state_path = db.config.data_dir / "_sleep_state.md"
    if not state_path.exists():
        return
    try:
        meta, _ = fm_parse(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, CodecError):
        return
    last = meta.get("last_homeostasis")
    if not isinstance(last, dict):
        return
    reconciled = int(last.get("reconciled", 0))
    removed = int(last.get("removed", 0))
    absorbed = int(last.get("absorbed", 0))
    parse_failures = int(last.get("parse_failures", 0))
    if reconciled or removed or parse_failures:
        severity = SEVERITY_WARN if (reconciled or parse_failures) else SEVERITY_INFO
        report.issues.append(
            DoctorIssue(
                code="homeostasis_activity",
                severity=severity,
                target="_sleep_state.md",
                message=(
                    f"last sleep homeostasis: absorbed={absorbed} "
                    f"removed={removed} reconciled={reconciled} "
                    f"parse_failures={parse_failures}"
                ),
            )
        )
    elif absorbed:
        report.info.append(
            DoctorIssue(
                code="homeostasis_activity",
                severity=SEVERITY_INFO,
                target="_sleep_state.md",
                message=f"last sleep absorbed={absorbed} (no reconcile/remove)",
            )
        )


def _check_english_naming(db: Database, report: DoctorReport) -> None:
    """Diagnose non-english folder/file names."""

    from mddbai.core.validation import is_english_identifier, suggest_english  # noqa: PLC0415

    root = Path(db.config.data_dir)
    if not root.exists():
        return

    seen: set[Path] = set()
    for entry in root.rglob("*"):
        rel_parts = entry.relative_to(root).parts
        if any(part.startswith("_") for part in rel_parts):
            continue
        if entry.is_file() and entry.name.startswith("_"):
            continue
        if entry.is_file() and (
            entry.name.endswith(".lock")
            or entry.name.endswith(".tmp")
            or entry.name.endswith(".bak")
        ):
            continue
        bad_part: str | None = None
        for part in rel_parts:
            if part.endswith(".lock") or part.endswith(".tmp") or part.endswith(".bak"):
                break
            stem = part[:-3] if part.endswith(".md") else part
            if not is_english_identifier(stem):
                bad_part = part
                break
        if bad_part is None:
            continue
        bad_dir = root.joinpath(*rel_parts[: rel_parts.index(bad_part) + 1])
        if bad_dir in seen:
            continue
        seen.add(bad_dir)
        stem = bad_part[:-3] if bad_part.endswith(".md") else bad_part
        suggestion = suggest_english(stem)
        report.issues.append(
            DoctorIssue(
                code="non_english_name",
                severity=SEVERITY_WARN,
                target=str(bad_dir.relative_to(root)).replace("\\", "/"),
                message=(
                    f"non-english identifier {bad_part!r} — rename to "
                    f"{suggestion!r} (or another a-z0-9_- name)"
                ),
            )
        )


# ---- gate mode (2026-05-07) ---------------------------------------------
#
# `mddbai doctor --gate` only *fails* on conditions the code can enforce.
# Things that live outside the mddbai process — external shells, Read,
# Grep, Select-String, whole-file reads — cannot be blocked from code, so
# they stay in rules/guides only. The gate validates *disk state* alone.


# Issue codes treated as critical in gate mode (regardless of severity, escalated to ERROR).
GATE_CRITICAL_CODES: frozenset[str] = frozenset(
    {
        # palace identity (_palace.md) missing / 4 fields incomplete — blocks cold-AI entry.
        "gate_palace_root_missing",
        "gate_palace_root_incomplete",
        # drawer flat-dump pattern (zero H2 sections) — V3 byte-alignment violation signal.
        "gate_flat_dump_drawer",
        # drawer body too large blows up the navigate first-read (1 MiB hard cap).
        "gate_drawer_navigation_hard_cap",
    }
)


def gate_check(db: Database) -> DoctorReport:
    """Gate-only minimal check — limited to conditions enforceable from code.

    Checks:
      G1. ``_palace.md`` exists and all 4 fields (purpose/scale/axes/fallback) filled — ERROR
      G2. If any drawer exists — zero drawers may be flat dumps (zero H2 sections) — ERROR
      G3. Drawer 50 MiB hard cap — ERROR
      G4. Front-door pass ratio (front_door_ratio) below FRONT_DOOR_RATIO_MIN — INFO (no rejection)

    If the data directory has zero drawers (right after init), G2/G3 are skipped (info).
    G1 is also INFO on empty directories (palace identity is opt-in before the first write).
    G4 is suppressed when call statistics are empty (vacuously true).

    Returns:
        ``DoctorReport`` whose ``is_healthy()`` indicates whether the gate passes.
    """

    report = DoctorReport(data_dir=str(db.config.data_dir))
    try:
        tables = [str(t) for t in db.tables()]
    except Exception:  # noqa: BLE001
        tables = []
    report.tables = sorted(set(tables))

    has_any_drawer = False
    for table_name in report.tables:
        base = db.config.data_dir / table_name
        if _iter_drawer_files(base):
            has_any_drawer = True
            break

    # G1 — palace identity check.
    palace_path = db.config.data_dir / "_palace.md"
    if not palace_path.exists():
        if has_any_drawer:
            report.issues.append(
                DoctorIssue(
                    code="gate_palace_root_missing",
                    severity=SEVERITY_ERROR,
                    target="_palace.md",
                    message=(
                        "palace identity missing while drawers exist — the cold AI entry path is broken. "
                        "Run `mddbai palace root-init <data_dir> --purpose ... --scale ... --axes ... --fallback ... --no-confirm` to write all 4 fields."
                    ),
                )
            )
        else:
            report.info.append(
                DoctorIssue(
                    code="gate_palace_root_missing",
                    severity=SEVERITY_INFO,
                    target="_palace.md",
                    message="empty data_dir — palace root not yet required (will be on first drawer write)",
                )
            )
    else:
        try:
            meta, _ = fm_parse(palace_path.read_text(encoding="utf-8"))
        except (OSError, CodecError, ValueError) as exc:
            report.issues.append(
                DoctorIssue(
                    code="gate_palace_root_incomplete",
                    severity=SEVERITY_ERROR,
                    target="_palace.md",
                    message=f"_palace.md parse failed: {type(exc).__name__}: {exc}",
                )
            )
        else:
            missing_fields: list[str] = []
            for key in ("purpose", "scale", "axes", "fallback"):
                value = meta.get(key)
                if not _has_navigation_value(value):
                    missing_fields.append(key)
            if missing_fields:
                report.issues.append(
                    DoctorIssue(
                        code="gate_palace_root_incomplete",
                        severity=SEVERITY_ERROR,
                        target="_palace.md",
                        message=(
                            f"_palace.md missing fields among the 4 required: {', '.join(missing_fields)}. "
                            "Use palace root-init to fill them all."
                        ),
                    )
                )

    # G2 — flat dump check (only when at least one drawer exists).
    if has_any_drawer:
        for table_name in report.tables:
            base = db.config.data_dir / table_name
            for path in _iter_drawer_files(base):
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                section_count = 0
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("## ") and not stripped.startswith("### "):
                        section_count += 1
                # Zero H2 sections + body >= 1 KB = flat dump pattern.
                size = path.stat().st_size if path.exists() else 0
                if section_count == 0 and size > 1024:
                    rel = _drawer_rel(base, path)
                    report.issues.append(
                        DoctorIssue(
                            code="gate_flat_dump_drawer",
                            severity=SEVERITY_ERROR,
                            target=f"{table_name}/{rel}",
                            message=(
                                f"drawer has zero H2 sections + {size:,} bytes flat dump — "
                                "V3 byte-alignment violation signal. Use `mddbai split-drawer` or "
                                "`ingest-document --split-by-heading` to add sections."
                            ),
                        )
                    )

    # G3 — drawer 50 MiB hard cap (block navigation first-read explosion).
    for table_name in report.tables:
        base = db.config.data_dir / table_name
        for path in _iter_drawer_files(base):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > _NAVIGATION_HARD_CAP_BYTES:
                rel = _drawer_rel(base, path)
                report.issues.append(
                    DoctorIssue(
                        code="gate_drawer_navigation_hard_cap",
                        severity=SEVERITY_ERROR,
                        target=f"{table_name}/{rel}",
                        message=(
                            f"drawer {size:,} bytes > 50 MiB hard cap — the navigate first-read "
                            "would explode. Call split-drawer immediately."
                        ),
                    )
                )

    # G4 — front-door pass ratio INFO (no rejection, hint only).
    _check_gate_front_door_ratio(db, report)

    return report


def _check_gate_front_door_ratio(db: Database, report: DoctorReport) -> None:
    """G4 — front-door pass ratio (front_door_ratio) INFO hint.

    Emit one INFO issue when the share of ``mddbai write`` / ``mddbai read``
    calls going through the front door is below FRONT_DOOR_RATIO_MIN. No
    rejection — old commands still work as before.
    Suppressed when call statistics are empty (vacuously true).
    """

    from mddbai.brain.tutorial import FRONT_DOOR_RATIO_MIN, front_door_ratio as _fdr  # noqa: PLC0415

    try:
        ratio = _fdr(db.config.data_dir)
    except Exception:  # noqa: BLE001
        return

    from mddbai.brain.tutorial import read_state  # noqa: PLC0415

    try:
        state = read_state(db.config.data_dir)
        if not state.recent_calls:
            return
    except Exception:  # noqa: BLE001
        return

    if ratio < FRONT_DOOR_RATIO_MIN:
        report.info.append(
            DoctorIssue(
                code="gate_front_door_ratio",
                severity=SEVERITY_INFO,
                target="_brain/_tutorial_state.md",
                message=(
                    f"front_door_ratio: {ratio:.2f} "
                    f"(routing through mddbai write/read makes the next recall easier). "
                    "Old commands keep working unchanged."
                ),
            )
        )


__all__ = [
    "DoctorIssue",
    "DoctorReport",
    "GATE_CRITICAL_CODES",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_WARN",
    "check",
    "gate_check",
]

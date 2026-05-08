from __future__ import annotations

"""Stage Z.1 — Drawer Store (1 file = N sections, prototype).

Memory palace metaphor refinement (decision 2026-05-02): one .md file = one drawer,
H2 section = a section of the drawer. Sidecar alternative to L.0.3 (one file = one record).

This module *does not touch the existing engine*. drawer_store / drawer_cache
/ drawer_engine 3 components run as a sidecar. Once self-usage testing passes, will be integrated in stage Z.6.

Storage format::

    ---
    _kind: drawer
    _drawer_id: facts/medicine
    ---
    ## tylenol
    Tylenol 500mg, pain relief.
    - created: 2026-05-01

    ## vitamin-d
    Vitamin D...

Principle alignment:
- Section ID is a key written by the caller (AI). drawer_store only provides the *placement location*
- atomic_write_text for read-modify-write. partial files are not visible
- FileLock per-drawer serializes concurrent upserts (D6 multi-agent alignment)
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import xxhash
import yaml

from mddbai.codec import frontmatter as fm
from mddbai.codec.sections import Section, split_sections
from mddbai.core.errors import CodecError
from mddbai.storage.atomic import atomic_write_text
from mddbai.storage.locks import FileLock

_SECTION_LEVEL = 2  # H2 = section

# Stage Z.7++.2 — write-time section index keys (body-relative offset, content hash)
_SECTIONS_META_KEY = "_sections"
_CONTENT_HASH_META_KEY = "_content_hash"


# Stage Z.7++.3 (2026-05-03) — flow-style list marker.
#
# Dumping the `_sections` list-of-list in yaml block style produces N sections x 3 lines =
# 3N lines of boilerplate embedded in frontmatter (23 sections -> 70 lines). Direct violation of
# R4 principle ("just a drawer laid open") + token principle ("every line = user payment").
#
# Solution: wrap _sections / inner entries with the list subclass `_FlowList` so the yaml
# representer serializes them inline (flow). yaml.safe_load result is a regular list,
# preserving compatibility with existing isinstance checks. body_hash is computed only from body,
# so yaml style changes are unrelated to the hash — external edit detection accuracy is preserved.
class _FlowList(list):  # type: ignore[type-arg]
    """list subclass that serializes in inline (flow) style on yaml dump."""


def _flow_list_representer(
    dumper: yaml.SafeDumper, data: _FlowList
) -> yaml.nodes.SequenceNode:
    return dumper.represent_sequence(
        "tag:yaml.org,2002:seq", data, flow_style=True
    )


yaml.SafeDumper.add_representer(_FlowList, _flow_list_representer)


@dataclass(frozen=True, slots=True)
class DrawerContents:
    """Parsed result of one drawer file.

    Attributes:
        meta: frontmatter dict (empty dict if missing).
        sections: list of H2 sections. Order of appearance preserved.
        prelude: body text before the first H2 (usually empty string).
    """

    meta: dict[str, Any]
    sections: list[Section]
    prelude: str

    def section_keys(self) -> list[str]:
        return [s.title for s in self.sections]

    def get(self, section_id: str) -> Section | None:
        for s in self.sections:
            if s.title == section_id:
                return s
        return None


# Stage U.9 (2026-05-08) — mmap-based read measurement results.
#
# Windows / 50MB measurement (NTFS): mmap is ~30% slower than read_text. Same even
# without CRLF normalization. Windows mmap overhead + file mapping setup is more expensive
# than the Python-optimized path of a single read_text syscall.
#
# Code path is kept (an opt-in slot for after Linux measurement). default OFF — threshold
# is set to an astronomical value. After Linux validation, replace threshold or default ON if meaningful.
_MMAP_THRESHOLD_BYTES = 1 << 62  # default OFF (env-based opt-in slot)


def read_drawer_text(path: Path) -> str:
    """Read drawer file as text.

    Stage U.9 (2026-05-08): attempted to introduce mmap fast-path at the 1MiB+ slot.
    Windows measurement showed it slower than read_text, so default OFF (threshold ``1 << 62``).
    After Linux measurement, this is the slot to replace ``_MMAP_THRESHOLD_BYTES`` if meaningful.

    byte-equivalent — both paths produce identical str after universal-newlines applied.

    Raises:
        FileNotFoundError: file missing.
    """

    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise
    if size < _MMAP_THRESHOLD_BYTES:
        return path.read_text(encoding="utf-8")
    # Large file — mmap (default OFF). Active when threshold is adjusted after Linux measurement.
    import mmap as _mmap

    with open(path, "rb") as f:
        with _mmap.mmap(f.fileno(), 0, access=_mmap.ACCESS_READ) as mm:
            text = mm[:].decode("utf-8")
    # universal-newlines alignment: same behavior as read_text (CRLF/CR -> LF)
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def read_drawer(path: Path) -> DrawerContents:
    """Parse a drawer file and decompose it into frontmatter + H2 section list.

    Returns empty DrawerContents if the file does not exist (for new drawers).
    """

    if not path.exists():
        return DrawerContents(meta={}, sections=[], prelude="")
    text = read_drawer_text(path)
    return _parse(text)


def _parse(text: str) -> DrawerContents:
    meta, body = fm.parse(text)
    # Stage Z.7++.2 — write-time trace is hidden from the caller as *internal computed metadata*.
    # render_drawer recomputes on every write so stale risk is 0. Visible in the user's cat
    # view, but not exposed in the Python API's `contents.meta`.
    meta = {
        k: v
        for k, v in meta.items()
        if k not in (_SECTIONS_META_KEY, _CONTENT_HASH_META_KEY)
    }
    raw_sections = split_sections(body)

    prelude = ""
    sections: list[Section] = []
    seen: set[str] = set()
    for sec in raw_sections:
        if sec.level == 0 and not sec.title:
            # prelude before the first H2 (split_sections represents it as an empty header)
            prelude = sec.content
            continue
        if sec.level != _SECTION_LEVEL:
            # H1 / H3+ are unsupported in the prototype — absorbed as sub-sections of the first section.
            # Policy decision in later rounds.
            if sections:
                last = sections[-1]
                merged_content = (
                    last.content + "\n" + _render_section(sec)
                ).strip("\n")
                sections[-1] = Section(
                    level=last.level, title=last.title, content=merged_content
                )
                continue
            # If no H2 exists at all, treat as prelude
            prelude = (
                prelude + ("\n" if prelude else "") + _render_section(sec)
            ).strip("\n")
            continue
        if sec.title in seen:
            raise CodecError(f"duplicate section id in drawer: {sec.title!r}")
        seen.add(sec.title)
        sections.append(sec)
    return DrawerContents(meta=meta, sections=sections, prelude=prelude)


def _render_section(section: Section) -> str:
    if not section.title and section.level == 0:
        return section.content
    header = "#" * section.level + " " + section.title
    if section.content:
        return header + "\n" + section.content
    return header


def render_drawer(contents: DrawerContents) -> str:
    """Serialize DrawerContents into markdown text.

    Stage Z.7++.2 (2026-05-03) — write-time trace: H2 section relative offsets in body
    and body content hash are embedded together in frontmatter (`_sections` /
    `_content_hash`). The next cold read can reuse the index using only hash verification
    without a line-by-line scan (drawer_cache._DrawerEntry.sections fast-path).

    Stage U.8 (2026-05-08) — incremental section index. H2 char offsets are computed
    accumulatively during join. The separate ``_compute_body_section_index`` full scan is removed.
    byte-equivalent (same offset definition as the scanner: next H2 line_start, last is body end).
    ~50% reduction in 50MB drawer render time.

    Principle alignment:
    - "embeddings are a detour — write-time trace is the answer" (2026-05-03 principle).
    - markdown SSOT preservation: index is embedded only in text frontmatter (no sidecar).
    - external edit safety: if hash breaks, the read side falls back automatically and rebuilds itself.
    """

    parts: list[str] = []
    sections_index: list[list[Any]] = []
    pos = 0  # body char offset accumulator

    if contents.prelude:
        prelude_clean = contents.prelude.rstrip("\n")
        parts.append(prelude_clean)
        pos += len(prelude_clean)

    for sec in contents.sections:
        if parts:
            # "\n\n" separator before this section (reflects join result as-is)
            pos += 2
        section_start = pos
        block = "## " + sec.title
        if sec.content:
            block += "\n" + sec.content.rstrip("\n")
        parts.append(block)
        pos += len(block)
        if sections_index:
            # close the previous section's end with *this section's line_start*
            # (same definition as scanner — separator "\n\n" is included in the previous section's range)
            sections_index[-1][2] = section_start
        sections_index.append([sec.title, section_start, pos])

    body = "\n\n".join(parts)
    if body and not body.endswith("\n"):
        body += "\n"

    # last section's end is body end (matches scanner behavior)
    if sections_index:
        sections_index[-1][2] = len(body)

    body_hash = xxhash.xxh64(body.encode("utf-8")).hexdigest()

    meta = {
        k: v
        for k, v in contents.meta.items()
        if k not in (_SECTIONS_META_KEY, _CONTENT_HASH_META_KEY)
    }
    # Index / hash placed consistently at the end of frontmatter — minimizes diff noise.
    # Stage Z.7++.3 (2026-05-03) — dump _sections in flow style to reduce cat view
    # boilerplate to 1/5+ (23 sections: 70 lines -> ~13 lines).
    meta[_SECTIONS_META_KEY] = _FlowList(_FlowList(entry) for entry in sections_index)
    meta[_CONTENT_HASH_META_KEY] = body_hash

    return fm.render(meta, body)


def _compute_body_section_index(body: str) -> tuple[list[list[Any]], str]:
    """Compute H2 section relative offsets + xxh64 hash from body text.

    Returns:
        (sections, body_hash) — sections is ``[[name, start_in_body,
        end_in_body], ...]`` in order of appearance. body_hash is xxh64 hex.

    Reason for relative offsets: even if frontmatter changes and body_offset shifts,
    the index itself is *computed only from body* so it remains stable (on re-render the
    _sections value can stay the same).
    """

    body_hash = xxhash.xxh64(body.encode("utf-8")).hexdigest()
    sections: list[list[Any]] = []
    if not body:
        return sections, body_hash
    in_fence = False
    pos = 0
    n = len(body)
    line_start = 0
    current: tuple[str, int] | None = None
    while pos <= n:
        if pos == n or body[pos] == "\n":
            line = body[line_start:pos]
            stripped = line.lstrip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
            elif (
                not in_fence
                and stripped.startswith("## ")
                and not stripped.startswith("### ")
            ):
                title = stripped[3:].strip()
                if title:
                    if current is not None:
                        sections.append([current[0], current[1], line_start])
                    current = (title, line_start)
            line_start = pos + 1
        if pos == n:
            break
        pos += 1
    if current is not None:
        sections.append([current[0], current[1], n])
    return sections, body_hash


def write_drawer(path: Path, contents: DrawerContents, *, fsync: bool = True) -> None:
    """Atomic whole-drawer write."""

    path.parent.mkdir(parents=True, exist_ok=True)
    text = render_drawer(contents)
    atomic_write_text(path, text, fsync=fsync)


def upsert_section_in_text(
    current_text: str,
    section_id: str,
    body: str,
    *,
    meta_update: dict[str, Any] | None = None,
) -> str:
    """Pure function — upsert section into in-memory text without disk IO.

    Entry point for stage Z.7++ (write-back). Called by ``DrawerCache`` when updating
    its dirty buffer. If ``current_text`` is an empty string, treated as a new drawer.

    Raises:
        ValueError: section_id is empty or contains a newline.
    """

    if not section_id or "\n" in section_id:
        raise ValueError(f"invalid section_id: {section_id!r}")

    if current_text:
        contents = _parse(current_text)
    else:
        contents = DrawerContents(meta={}, sections=[], prelude="")
    new_meta = dict(contents.meta)
    if meta_update:
        new_meta.update(meta_update)
    new_section = Section(
        level=_SECTION_LEVEL, title=section_id, content=body.rstrip("\n")
    )
    existing_idx = next(
        (i for i, s in enumerate(contents.sections) if s.title == section_id),
        None,
    )
    sections = list(contents.sections)
    if existing_idx is None:
        sections.append(new_section)
    else:
        sections[existing_idx] = new_section
    new_contents = DrawerContents(
        meta=new_meta, sections=sections, prelude=contents.prelude
    )
    return render_drawer(new_contents)


def delete_section_in_text(current_text: str, section_id: str) -> tuple[str, bool]:
    """Pure function — remove one section from in-memory text.

    Entry point for stage Z.7++ (write-back).

    Returns:
        ``(new_text, removed)``. If the section did not exist, ``(current_text, False)``.
    """

    if not current_text:
        return current_text, False
    contents = _parse(current_text)
    idx = next(
        (i for i, s in enumerate(contents.sections) if s.title == section_id), None
    )
    if idx is None:
        return current_text, False
    sections = list(contents.sections)
    sections.pop(idx)
    new_contents = DrawerContents(
        meta=contents.meta, sections=sections, prelude=contents.prelude
    )
    return render_drawer(new_contents), True


def upsert_section(
    path: Path,
    section_id: str,
    body: str,
    *,
    meta_update: dict[str, Any] | None = None,
    fsync: bool = True,
    lock_timeout_s: float = 30.0,
    prefetched_text: tuple[str, int] | None = None,
) -> tuple[str, int]:
    """Update a single drawer section via read-modify-write.

    Args:
        path: drawer .md path.
        section_id: H2 header (`## <section_id>`).
        body: section body (excluding header). Trailing newline handled automatically.
        meta_update: keys to merge into frontmatter. If None, existing kept.
        fsync: fsync option for atomic_write_text.
        lock_timeout_s: lock wait limit for concurrent upserts.
        prefetched_text: stage Z.7+ write-through optimization. If the caller
            (typically ``DrawerEngine``) passes the cache-hit text along with mtime_ns,
            after acquiring lock, compares with path.stat().st_mtime_ns and if *unchanged*,
            parses the cached text to save 1 disk read. On mtime mismatch / file
            absence, falls back to the original path (disk read).

    Raises:
        ValueError: section_id is empty or contains a newline.
    """

    if not section_id or "\n" in section_id:
        raise ValueError(f"invalid section_id: {section_id!r}")

    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(path, timeout_s=lock_timeout_s):
        contents: DrawerContents | None = None
        if prefetched_text is not None and path.exists():
            text, cached_mtime = prefetched_text
            try:
                if path.stat().st_mtime_ns == cached_mtime:
                    contents = _parse(text)
            except OSError:
                pass
        if contents is None:
            contents = read_drawer(path)
        new_meta = dict(contents.meta)
        # Logic below is unchanged — capture result_text after write and return
        # (so the engine can use it on cache update without a new disk read)
        if meta_update:
            new_meta.update(meta_update)
        new_section = Section(
            level=_SECTION_LEVEL, title=section_id, content=body.rstrip("\n")
        )
        existing_idx = next(
            (i for i, s in enumerate(contents.sections) if s.title == section_id),
            None,
        )
        sections = list(contents.sections)
        if existing_idx is None:
            sections.append(new_section)
        else:
            sections[existing_idx] = new_section
        new_contents = DrawerContents(
            meta=new_meta, sections=sections, prelude=contents.prelude
        )
        new_text = render_drawer(new_contents)
        atomic_write_text(path, new_text, fsync=fsync)
        try:
            new_mtime = path.stat().st_mtime_ns
        except OSError:
            new_mtime = 0
        return new_text, new_mtime


def delete_section(
    path: Path,
    section_id: str,
    *,
    fsync: bool = True,
    lock_timeout_s: float = 30.0,
) -> bool:
    """Remove one section. Returns False if it does not exist.

    If the drawer ends up with 0 sections, the file itself is kept but only prelude +
    frontmatter remains (the user can see it is empty by cat).
    """

    if not path.exists():
        return False
    with FileLock(path, timeout_s=lock_timeout_s):
        contents = read_drawer(path)
        idx = next(
            (i for i, s in enumerate(contents.sections) if s.title == section_id),
            None,
        )
        if idx is None:
            return False
        sections = list(contents.sections)
        sections.pop(idx)
        new_contents = DrawerContents(
            meta=contents.meta, sections=sections, prelude=contents.prelude
        )
        write_drawer(path, new_contents, fsync=fsync)
        return True


def list_sections(path: Path) -> list[str]:
    """List of section IDs inside a drawer (in order of appearance)."""

    if not path.exists():
        return []
    return read_drawer(path).section_keys()


def get_section(path: Path, section_id: str) -> Section | None:
    """Read and return a single drawer section directly from disk.

    Path that does not use drawer_cache (cold path). For bulk calls, prefer routing
    via drawer_engine.take_section.
    """

    if not path.exists():
        return None
    return read_drawer(path).get(section_id)


def split_drawer(
    path: Path,
    plan: dict[str, list[str]],
    *,
    fsync: bool = True,
    lock_timeout_s: float = 30.0,
    extra_meta: dict[str, Any] | None = None,
) -> list[Path]:
    """Split one drawer into N new drawers according to ``plan`` (stage Z.7).

    ``plan`` is a ``{<new_drawer_path>: [<section_id>, ...]}`` mapping. All key paths are
    interpreted as new .md files in the *same folder* as the source, and every section must be
    relocated exactly once (the split is enforced lossless).

    Behavior:
    1. Acquire FileLock on the source drawer (blocks concurrent put_section)
    2. Read source -> extract all sections
    3. Validate partition — sum of plan sections must equal source section set
    4. Write each new drawer with atomic_write_text (frontmatter ``_split_from`` /
       ``_split_at`` embedded automatically)
    5. On full success, unlink the source file (cache invalidation is the caller's responsibility)

    Args:
        path: source drawer path.
        plan: mapping of new drawer -> list of section IDs. Keys are relative paths under
              the same parent folder as the source (".md" suffix automatic) or absolute paths.
              Order of values determines section order inside the new drawer.
        fsync: fsync option for atomic_write_text.
        lock_timeout_s: lock wait limit.
        extra_meta: additional keys to merge into new drawer frontmatter (e.g. split_reason).

    Returns:
        List of new drawer paths actually created on disk.

    Raises:
        FileNotFoundError: source drawer missing.
        ValueError: plan is empty or partition is not lossless (missing / duplicate /
                    section not in source / path key same as source).
    """

    if not plan:
        raise ValueError("split plan must contain at least one new drawer")

    if not path.exists():
        raise FileNotFoundError(str(path))

    # Normalize new drawer paths — interpret as .md files under the same parent folder
    parent = path.parent
    normalized_plan: dict[Path, list[str]] = {}
    for raw_name, sec_ids in plan.items():
        if not raw_name:
            raise ValueError("new drawer name must not be empty")
        if not sec_ids:
            raise ValueError(f"new drawer {raw_name!r} has no sections assigned")
        new_path = Path(raw_name)
        if not new_path.is_absolute():
            new_path = parent / new_path
        if not new_path.suffix:
            new_path = new_path.with_suffix(".md")
        if new_path == path:
            raise ValueError(
                f"new drawer path {new_path} collides with source drawer"
            )
        normalized_plan[new_path] = list(sec_ids)

    # path collision in plan (two distinct keys normalized to the same file)
    if len(normalized_plan) != len(plan):
        raise ValueError("plan contains duplicate normalized paths")

    with FileLock(path, timeout_s=lock_timeout_s):
        contents = read_drawer(path)
        original_keys = contents.section_keys()
        original_set = set(original_keys)

        # partition validation
        seen: set[str] = set()
        for new_path, sec_ids in normalized_plan.items():
            for sid in sec_ids:
                if sid in seen:
                    raise ValueError(f"section {sid!r} assigned to multiple drawers")
                if sid not in original_set:
                    raise ValueError(
                        f"section {sid!r} not present in source drawer"
                    )
                seen.add(sid)
        missing = original_set - seen
        if missing:
            raise ValueError(
                f"split plan must cover all sections; missing: {sorted(missing)!r}"
            )

        # section_id -> Section mapping
        by_id: dict[str, Section] = {s.title: s for s in contents.sections}

        # Write the new drawers atomically. If any fails, partial new drawers
        # may remain on disk (distributed atomicity is not provided), but the source
        # is not touched until the last step — recoverability is guaranteed.
        from mddbai.core.clock import SystemClock  # noqa: PLC0415

        split_at_ns = SystemClock().now_ns()
        written: list[Path] = []
        for new_path, sec_ids in normalized_plan.items():
            new_meta: dict[str, Any] = dict(contents.meta)
            new_meta["_kind"] = "drawer"
            new_meta["_drawer_id"] = _drawer_id_from_path(new_path, parent, contents)
            new_meta["_split_from"] = contents.meta.get(
                "_drawer_id", path.name
            )
            new_meta["_split_at"] = int(split_at_ns)
            if extra_meta:
                new_meta.update(extra_meta)
            new_sections = [by_id[sid] for sid in sec_ids]
            new_contents = DrawerContents(
                meta=new_meta, sections=new_sections, prelude=""
            )
            write_drawer(new_path, new_contents, fsync=fsync)
            written.append(new_path)

        # Unlink source. Only after all writes succeed.
        path.unlink()

    return written


_DATE_PATTERN = re.compile(r"(?P<base>.*?)(?P<date>\d{4}-\d{2}-\d{2})$")
_YEAR_MONTH_PATTERN = re.compile(r"(?P<base>.*?)(?P<ym>\d{4}-\d{2})$")


def plan_split_by_time(path: Path) -> dict[str, list[str]]:
    """Generate a plan to split one drawer into *time-ordered halves* (stage Z.7).

    Section appearance order in the file is *assumed* to be time order (since the user
    appends new sections from top to bottom, this is a natural inference). Splits in half
    and decides the two new drawer names with a *lightweight* heuristic:

    - if the drawer stem ends with ``YYYY-MM-DD``, ``-am`` / ``-pm``
    - if it ends with ``YYYY-MM``, ``-h1`` / ``-h2``
    - otherwise ``-part1`` / ``-part2``

    Semantic splitting is never automatic — always halved (D2 alignment).

    Returns:
        ``{new_drawer_path: [section_ids]}`` mapping. An empty drawer (0 / 1 section)
        has no meaningful split -> ValueError.

    Raises:
        FileNotFoundError: drawer missing.
        ValueError: section count <= 1 (cannot split).
    """

    if not path.exists():
        raise FileNotFoundError(str(path))
    contents = read_drawer(path)
    keys = contents.section_keys()
    if len(keys) < 2:
        raise ValueError(
            f"drawer {path} has {len(keys)} section(s); need >=2 to split"
        )

    mid = (len(keys) + 1) // 2  # when odd, more goes to the first half
    first, second = keys[:mid], keys[mid:]

    stem = path.stem
    if _DATE_PATTERN.fullmatch(stem):
        suffix_a, suffix_b = "am", "pm"
    elif _YEAR_MONTH_PATTERN.fullmatch(stem):
        suffix_a, suffix_b = "h1", "h2"
    else:
        suffix_a, suffix_b = "part1", "part2"

    return {
        f"{stem}-{suffix_a}.md": first,
        f"{stem}-{suffix_b}.md": second,
    }


def _drawer_id_from_path(
    new_path: Path, parent: Path, contents: DrawerContents
) -> str:
    """Infer the ``_drawer_id`` for a new drawer.

    If the source's ``_drawer_id`` has the form ``<table>/<drawer>``, preserve the ``<table>`` prefix.
    Otherwise use only the new file name (extension removed).
    """

    stem = new_path.stem
    src = contents.meta.get("_drawer_id")
    if isinstance(src, str) and "/" in src:
        table_part, _, _ = src.rpartition("/")
        try:
            rel = new_path.relative_to(parent).with_suffix("").as_posix()
            return f"{table_part}/{rel}"
        except ValueError:
            return f"{table_part}/{stem}"
    return stem


# =============================================================================
# Stage Z.8 (2026-05-03) — multi-process safe delta sidecar
# =============================================================================
#
# Background: the process-local cache of Z.7++ write-back has 70% loss in a multi-process
#       environment (stage X measurement). mtime conflicts at flush time were dropped.
#
# Solution: borrow the Git branch + merge pattern. Each process writes only to its own
#       ULID-stamped delta file -> 0 conflicts. Read merges main + all deltas with last-wins.
#       Sleep work absorbs them into main in the background.
#
# Disk layout::
#
#     <table>/<drawer>.md                          # main (consolidated state)
#     <table>/_drawers/<drawer>/                   # delta directory
#         delta-<lsn1>.md                          # process 1's accumulated changes
#         delta-<lsn2>.md                          # process 2's accumulated changes
#
# delta file schema (same structure as a regular drawer + extra metadata)::
#
#     ---
#     _kind: drawer_delta
#     _drawer_id: <table>/<drawer>
#     _lsn: <ulid>
#     _target: <main_filename>
#     _deleted_sections: [sec1, sec2]    # optional — this delta marks them deleted
#     ---
#     ## section-A
#     This process's new body...
#
# Principle alignment:
# - D5 (consolidation) — the sleep merge work is the main job. Daily = delta accumulation, consolidation = sleep
# - D6 (multi-agent first-class) — 0 locks, true parallel writes
# - D7 (drawer metaphor) — each writes on a memo and gathers into one drawer
# - token equivalence — the read view is last-wins of main + deltas, each section body byte-identical

DELTA_DIR_NAME = "_drawers"
DELTA_KIND = "drawer_delta"
DELTA_FILE_PREFIX = "delta-"
DELTA_FILE_SUFFIX = ".md"
DELETED_SECTIONS_META_KEY = "_deleted_sections"
DELTA_LSN_META_KEY = "_lsn"
DELTA_TARGET_META_KEY = "_target"

# System metadata keys — distinguished from *user metadata* during delta merge. Keys in this set
# are frame metadata embedded per-delta, not traces (D7 vividness) written by the user.
# When merge_view overlays user meta from deltas onto base meta with last-wins,
# keys in this set are excluded (delta frame must not flow into main).
SYSTEM_META_KEYS: frozenset[str] = frozenset({
    "_kind",
    "_sections",
    "_content_hash",
    DELTA_LSN_META_KEY,
    DELTA_TARGET_META_KEY,
    DELETED_SECTIONS_META_KEY,
})
# Note: ``_drawer_id`` is a system key but it is *drawer identity* — the delta's value must be
# promoted to main so that, in the new drawer (main missing -> first delta only) scenario, it
# survives merge into main. So it is *not put* into SYSTEM_META_KEYS (absorbed via the same
# last-wins path as user meta).


def delta_dir_for(main_path: Path) -> Path:
    """Delta directory path for a main drawer.

    Example: ``<table>/foo.md`` -> ``<table>/_drawers/foo/``.
    """

    return main_path.parent / DELTA_DIR_NAME / main_path.stem


def process_delta_path(main_path: Path, session_lsn: str) -> Path:
    """Per-process ULID delta file path."""

    return delta_dir_for(main_path) / f"{DELTA_FILE_PREFIX}{session_lsn}{DELTA_FILE_SUFFIX}"


def list_delta_files(main_path: Path) -> list[Path]:
    """Return all delta files of a drawer, LSN-sorted (ULID = time order)."""

    d = delta_dir_for(main_path)
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.name.startswith(DELTA_FILE_PREFIX) and p.suffix == DELTA_FILE_SUFFIX)


def delta_dir_mtime_ns(main_path: Path) -> int:
    """mtime of the delta directory (0 if missing). Used to detect new delta add/remove."""

    d = delta_dir_for(main_path)
    try:
        return d.stat().st_mtime_ns
    except FileNotFoundError:
        return 0


def merge_view(main_path: Path) -> DrawerContents:
    """Logical state of main + all deltas merged in LSN-order last-wins.

    Per-delta processing order:
    1. Remove sections in ``_deleted_sections`` metadata
    2. Apply delta H2 sections (overwrite existing, append new ones at end)

    Returns:
        Merged DrawerContents. If main is missing + deltas exist, synthesized from deltas alone.
    """

    if main_path.exists():
        base = read_drawer(main_path)
    else:
        base = DrawerContents(meta={}, sections=[], prelude="")

    deltas = list_delta_files(main_path)
    if not deltas:
        return base

    merged_meta = dict(base.meta)
    sections_by_id: dict[str, Section] = {s.title: s for s in base.sections}
    order: list[str] = [s.title for s in base.sections]

    for d_path in deltas:
        try:
            d_text = d_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        d_meta, d_body = fm.parse(d_text)
        # Validate _kind on delta too — skip if different kind (safety net)
        if d_meta.get("_kind") not in (DELTA_KIND, None):
            continue
        # Overlay delta's user meta onto base meta with last-wins (preserves D7 vividness).
        # System keys (_lsn / _target / _deleted_sections / _kind / _sections /
        # _content_hash) are delta frames and must not flow into main.
        for k, v in d_meta.items():
            if k in SYSTEM_META_KEYS:
                continue
            merged_meta[k] = v
        deleted = d_meta.get(DELETED_SECTIONS_META_KEY) or []
        if isinstance(deleted, list):
            for did in deleted:
                if isinstance(did, str):
                    sections_by_id.pop(did, None)
                    if did in order:
                        order.remove(did)
        # parse delta body sections
        try:
            d_contents = _parse(d_text)
        except CodecError:
            continue
        for sec in d_contents.sections:
            if sec.title not in sections_by_id:
                order.append(sec.title)
            sections_by_id[sec.title] = sec

    final_sections = [sections_by_id[k] for k in order if k in sections_by_id]
    return DrawerContents(meta=merged_meta, sections=final_sections, prelude=base.prelude)


def render_delta(
    drawer_id: str,
    target_filename: str,
    lsn: str,
    sections: list[Section],
    *,
    deleted_sections: list[str] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> str:
    """Serialize one delta file. Same format as main's ``render_drawer`` + extra metadata.

    Args:
        drawer_id: ``<table>/<drawer>`` form.
        target_filename: name of the main file to be merged (e.g. ``foo.md``).
        lsn: ULID of this delta.
        sections: sections this delta changed (overwrite or new).
        deleted_sections: section IDs marked deleted by this delta.
        extra_meta: additional frontmatter metadata.
    """

    meta: dict[str, Any] = {
        "_kind": DELTA_KIND,
        "_drawer_id": drawer_id,
        DELTA_LSN_META_KEY: lsn,
        DELTA_TARGET_META_KEY: target_filename,
    }
    if deleted_sections:
        meta[DELETED_SECTIONS_META_KEY] = list(deleted_sections)
    if extra_meta:
        meta.update(extra_meta)
    contents = DrawerContents(meta=meta, sections=sections, prelude="")
    return render_drawer(contents)


def write_delta(
    main_path: Path,
    session_lsn: str,
    *,
    drawer_id: str,
    sections: list[Section],
    deleted_sections: list[str] | None = None,
    fsync: bool = True,
    extra_meta: dict[str, Any] | None = None,
) -> Path:
    """Atomic whole-write to the process's delta file.

    The next flush of the same drawer in the same process *overwrites the same delta path* —
    accumulated changes from one process gather into a single delta file (prevents file count explosion).

    Returns:
        Path of the delta file written.
    """

    delta_path = process_delta_path(main_path, session_lsn)
    delta_path.parent.mkdir(parents=True, exist_ok=True)
    text = render_delta(
        drawer_id=drawer_id,
        target_filename=main_path.name,
        lsn=session_lsn,
        sections=sections,
        deleted_sections=deleted_sections,
        extra_meta=extra_meta,
    )
    atomic_write_text(delta_path, text, fsync=fsync)
    return delta_path


def consolidate_drawer_deltas(
    main_path: Path,
    *,
    fsync: bool = True,
    lock_timeout_s: float = 30.0,
) -> int:
    """Absorb all of a drawer's deltas into main + unlink deltas. Atomicity guaranteed.

    Procedure:
    1. Acquire main's FileLock (blocks concurrent put — adding new deltas uses a different lock)
    2. Compute merged result via merge_view
    3. atomic_write_text into main
    4. Unlink only the delta files that *existed at* absorption time (do not re-list, since
       new deltas may have been added in the meantime)

    Returns:
        Number of delta files absorbed. 0 if no deltas, or if main missing + deltas missing.
    """

    deltas_before = list_delta_files(main_path)
    if not deltas_before and not main_path.exists():
        return 0
    if not deltas_before:
        return 0

    main_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(main_path, timeout_s=lock_timeout_s):
        # Re-list deltas inside the lock — new deltas may have been added while acquiring
        deltas = list_delta_files(main_path)
        if not deltas:
            return 0
        merged = merge_view(main_path)
        # preserve _kind/_drawer_id — standard metadata when written into main
        merged_meta = dict(merged.meta)
        merged_meta.setdefault("_kind", "drawer")
        new_main_contents = DrawerContents(
            meta=merged_meta, sections=merged.sections, prelude=merged.prelude
        )
        write_drawer(main_path, new_main_contents, fsync=fsync)
        # unlink only absorbed deltas
        absorbed = 0
        for d in deltas:
            try:
                d.unlink()
                absorbed += 1
            except FileNotFoundError:
                continue
            except OSError:
                # if unlink fails, continue — retried on the next cycle
                continue

    # Clean up the delta directory if empty
    dd = delta_dir_for(main_path)
    try:
        if dd.exists() and not any(dd.iterdir()):
            dd.rmdir()
    except OSError:
        pass

    return absorbed


def is_delta_path(path: Path) -> bool:
    """True if the path matches the delta file pattern (used to skip in drawer listings)."""

    if path.suffix != DELTA_FILE_SUFFIX:
        return False
    if not path.name.startswith(DELTA_FILE_PREFIX):
        return False
    # Parent's parent folder must be _drawers to be a real delta
    parts = path.parts
    return DELTA_DIR_NAME in parts


__all__ = [
    "DELETED_SECTIONS_META_KEY",
    "DELTA_DIR_NAME",
    "DELTA_KIND",
    "DELTA_FILE_PREFIX",
    "DELTA_FILE_SUFFIX",
    "DELTA_LSN_META_KEY",
    "DELTA_TARGET_META_KEY",
    "DrawerContents",
    "consolidate_drawer_deltas",
    "delete_section",
    "delete_section_in_text",
    "delta_dir_for",
    "delta_dir_mtime_ns",
    "get_section",
    "is_delta_path",
    "list_delta_files",
    "list_sections",
    "merge_view",
    "plan_split_by_time",
    "process_delta_path",
    "read_drawer",
    "read_drawer_text",
    "render_delta",
    "render_drawer",
    "split_drawer",
    "upsert_section",
    "upsert_section_in_text",
    "write_delta",
    "write_drawer",
    "_CONTENT_HASH_META_KEY",
    "_SECTIONS_META_KEY",
    "_compute_body_section_index",
]

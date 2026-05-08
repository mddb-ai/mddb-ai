from __future__ import annotations

"""Stage Z.2 — Drawer Cache (RAM LRU + section index, prototype).

A lazy RAM cache of the *body text itself* of the drawer model
(1 file = N sections). Sits a layer on top of shard_cache (Q.3, path meta).

Principles:
- Token-equivalence invariant: cache hit text == disk read text (byte for byte)
- mtime invalidation: automatically detect changes from external tools or
  other processes (D2 aligned)
- Explicit invalidation: callers invalidate after drawer_engine's put_section
- Bounded LRU: memory cap (default 64 MB)
- No TTL (same as Q.3 — avoids the D2 violation)

Internal structure::

    cache[path] = _DrawerEntry(
        text="---\\n...\\n## tylenol\\n...",
        sections={"tylenol": (start_byte, end_byte), ...},
        prelude_text="",  # if needed
        mtime_ns=...,
        size_bytes=...,
    )

`get_section(path, section_id)` returns only the `text[start:end]` slice —
zero extra memory copy (Python str slicing copies the data, but the per-call
cost is in nanoseconds).
"""

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

import xxhash

from mddbai.codec import frontmatter as fm
from mddbai.codec.sections import Section
from mddbai.core.errors import ConflictError
from mddbai.core.ulid import new_ulid
from mddbai.storage import drawer_store as _ds
from mddbai.storage.atomic import atomic_write_text
from mddbai.storage.locks import FileLock

_MAX_BYTES_DEFAULT = 64 * 1024 * 1024  # 64 MB
_DELETED = object()  # sentinel for deleted-section overlay marker


@dataclass(slots=True)
class _DrawerEntry:
    """One row of the cache.

    ``text`` is the base text from disk (when clean, the last on-disk
    state; when dirty, the base — without the overlay applied). The
    index ``_sections`` is *lazy* — built on first read.

    Stage Z.7++.1 (2026-05-03, per-section dirty overlay):
      - When ``dirty`` is True, the *logical state* is expressed as the
        combination of ``_overlay`` (per-section changes),
        ``_overlay_meta`` (frontmatter changes), and
        ``_overlay_new_order`` (insertion order of new sections).
      - ``apply_section`` / ``apply_delete`` update a single overlay
        slot in O(1). The text is not re-rendered (removing the N^2 cost
        of the previous Z.7++).
      - Only at ``flush`` are base + overlay rendered once and
        atomic_write'd.
      - Reads (``get_section`` etc.) check the overlay first, falling
        back to slicing the base text — guarantees read-your-writes.

    ``mtime_ns`` is the mtime when the base was loaded from disk
    (= conflict-validation baseline). Updated to the new mtime after
    flush.
    """

    text: str  # base text (when clean: main + merged deltas; when dirty: base without overlay)
    mtime_ns: int  # main file mtime (0 if absent)
    size_bytes: int
    dirty: bool = False
    # Stage Z.8 (2026-05-03) — mtime of the delta directory. When other
    # processes add a new delta, this value changes so the cache fails
    # its stale check. 0 = delta dir absent.
    delta_dir_mtime_ns: int = 0
    # Z.7++.1: per-section overlay. Values are body strings or the
    # _DELETED sentinel. _DELETED means the base section is *removed*.
    # None is not used.
    _overlay: dict[str, Any] | None = None
    _overlay_meta: dict[str, Any] | None = None
    # Insertion order of new sections (those not in base) — append
    # order during flush render.
    _overlay_new_order: list[str] | None = None
    # Incremental byte tracking (O(1) bytes_held sync — avoids N^2)
    _overlay_bytes: int = 0
    _sections: dict[str, tuple[int, int]] | None = None
    _body_offset: int = 0
    _indexed: bool = False

    def sections(self) -> dict[str, tuple[int, int]]:
        if not self._indexed:
            fast = _try_fast_section_index(self.text)
            if fast is not None:
                self._sections, self._body_offset = fast
            else:
                self._sections, self._body_offset = _build_section_index(self.text)
            self._indexed = True
        assert self._sections is not None
        return self._sections

    @property
    def body_offset(self) -> int:
        if not self._indexed:
            self.sections()
        return self._body_offset

    def replace_base(self, new_text: str, new_mtime_ns: int) -> int:
        """Called when the base is replaced (e.g. after flush). Clears
        the overlay and invalidates the index."""

        new_size = len(new_text.encode("utf-8"))
        diff = new_size - self.size_bytes
        self.text = new_text
        self.mtime_ns = new_mtime_ns
        self.size_bytes = new_size
        self.dirty = False
        self._overlay = None
        self._overlay_meta = None
        self._overlay_new_order = None
        self._overlay_bytes = 0
        self._sections = None
        self._body_offset = 0
        self._indexed = False
        return diff

    # ----- Z.7++.1 overlay helpers --------------------------------------

    def _ensure_overlay(self) -> None:
        if self._overlay is None:
            self._overlay = {}
            self._overlay_new_order = []

    def get_overlay_body(self, section_id: str) -> Any:
        """Look up the section's state in the overlay. Returns:
        - None: not in the overlay (base wins)
        - _DELETED: the overlay marks the base section as removed
        - str: the overlay body
        """

        if self._overlay is None:
            return None
        return self._overlay.get(section_id)

    def apply_overlay_section(
        self, section_id: str, body: str, meta_update: dict[str, Any] | None
    ) -> int:
        """Update the overlay. Returns the *delta* used to keep byte_held in sync (O(1))."""

        self._ensure_overlay()
        assert self._overlay is not None
        assert self._overlay_new_order is not None
        # Compute the byte delta from previous to new value
        prev = self._overlay.get(section_id)
        if isinstance(prev, str):
            prev_bytes = len(prev.encode("utf-8"))
        else:
            prev_bytes = 0
        new_bytes = len(body.encode("utf-8"))
        delta = new_bytes - prev_bytes
        # Track insertion order for new sections (O(1) — only base index lookup)
        if section_id not in self._overlay:
            base_keys = self.sections()
            if section_id not in base_keys:
                self._overlay_new_order.append(section_id)
        self._overlay[section_id] = body
        self._overlay_bytes += delta
        if meta_update:
            if self._overlay_meta is None:
                self._overlay_meta = {}
            self._overlay_meta.update(meta_update)
        self.dirty = True
        return delta

    def apply_overlay_delete(self, section_id: str) -> tuple[bool, int]:
        """Mark a deletion in the overlay. Returns ``(removed, byte_delta)``.

        removed is True only if the section *logically* existed.
        byte_delta is non-positive.
        """

        existed = self.has_logical_section(section_id)
        if not existed:
            return False, 0
        self._ensure_overlay()
        assert self._overlay is not None
        assert self._overlay_new_order is not None
        prev = self._overlay.get(section_id)
        if isinstance(prev, str):
            prev_bytes = len(prev.encode("utf-8"))
        else:
            prev_bytes = 0
        # Section was added then deleted -> drop from the overlay entirely
        if (
            section_id in self._overlay_new_order
            and section_id not in self.sections()
        ):
            self._overlay.pop(section_id, None)
            self._overlay_new_order.remove(section_id)
            delta = -prev_bytes
        else:
            self._overlay[section_id] = _DELETED
            delta = -prev_bytes  # _DELETED sentinel takes 0 bytes
        self._overlay_bytes += delta
        self.dirty = True
        return True, delta

    def has_logical_section(self, section_id: str) -> bool:
        """Whether the section exists in the *logical* state (base + overlay)."""

        if self._overlay is not None and section_id in self._overlay:
            return self._overlay[section_id] is not _DELETED
        return section_id in self.sections()

    def logical_size_bytes(self) -> int:
        """Stage II — *estimated* logical size (bytes) of base + overlay.

        Before flush we don't know the exact rendered size (computing it
        is expensive). Instead we approximate as ``size_bytes`` (base) +
        ``_overlay_bytes`` (overlay net delta). It is enough for the AI
        to read the split-threshold-near signal.
        """

        return max(0, self.size_bytes + self._overlay_bytes)

    def logical_section_count(self) -> int:
        """Stage II — logical section count (base + overlay combined, _DELETED excluded)."""

        return len(self.logical_section_keys())

    def logical_section_keys(self) -> list[str]:
        """List of *logical* section IDs (base order -> new-section order)."""

        base_keys = list(self.sections().keys())
        if self._overlay is None:
            return base_keys
        out: list[str] = []
        for k in base_keys:
            v = self._overlay.get(k, None)
            if v is _DELETED:
                continue
            out.append(k)
        if self._overlay_new_order:
            for k in self._overlay_new_order:
                v = self._overlay.get(k)
                if v is _DELETED or v is None:
                    continue
                out.append(k)
        return out

    def logical_get_section(self, section_id: str) -> str | None:
        """The section's text (header included) in the *logical* state. None if missing."""

        if self._overlay is not None and section_id in self._overlay:
            v = self._overlay[section_id]
            if v is _DELETED:
                return None
            assert isinstance(v, str)
            header = "## " + section_id
            if v:
                return f"{header}\n{v.rstrip(chr(10))}"
            return header
        offsets = self.sections().get(section_id)
        if offsets is None:
            return None
        start, end = offsets
        return self.text[start:end].rstrip("\n")

    def render_logical(self) -> str:
        """Combine base + overlay into the final text to write to disk. Used only at flush."""

        if not self.dirty and not self._overlay:
            return self.text
        if self.text:
            base_contents = _ds._parse(self.text)
        else:
            base_contents = _ds.DrawerContents(meta={}, sections=[], prelude="")
        meta = dict(base_contents.meta)
        if self._overlay_meta:
            meta.update(self._overlay_meta)
        new_sections: list[Section] = []
        for sec in base_contents.sections:
            ov = self._overlay.get(sec.title) if self._overlay else None
            if ov is _DELETED:
                continue
            if isinstance(ov, str):
                new_sections.append(
                    Section(level=2, title=sec.title, content=ov.rstrip("\n"))
                )
            else:
                new_sections.append(sec)
        if self._overlay and self._overlay_new_order:
            for sid in self._overlay_new_order:
                ov = self._overlay.get(sid)
                if ov is _DELETED or ov is None:
                    continue
                assert isinstance(ov, str)
                new_sections.append(
                    Section(level=2, title=sid, content=ov.rstrip("\n"))
                )
        new_contents = _ds.DrawerContents(
            meta=meta, sections=new_sections, prelude=base_contents.prelude
        )
        return _ds.render_drawer(new_contents)


@dataclass(slots=True)
class DrawerCacheStats:
    hits: int = 0
    misses: int = 0
    invalidations: int = 0
    evictions: int = 0
    bytes_held: int = 0


# The fixed overhead of yaml.safe_load parsing the frontmatter `_sections`
# list is more expensive than the slow scan on small drawers.
#
# Stage Z.7++.4 (2026-05-03) — adjusted threshold from 32 KB -> **64 KB**.
#   Rationale: in plans/ dogfood measurements, a 48 KB / 23-section drawer
#   (01-core-insight.md) regressed to ~0.97x — just above the 32 KB
#   threshold, the yaml parse list-of-list cost ate almost all of the
#   slow-scan win. Raising the threshold to 64 KB drops that range into
#   the slow path so it matches *legacy behavior exactly* (1.00x
#   guaranteed). The main case (>100KB) is still 3~5x faster.
_FAST_PATH_MIN_TEXT_BYTES = 64 * 1024


def _try_fast_section_index(
    text: str,
    *,
    min_bytes: int | None = None,
) -> tuple[dict[str, tuple[int, int]], int] | None:
    """Stage Z.7++.2 — write-time trace (`_sections` + `_content_hash`) fast-path.

    If a drawer is exactly as it was last written through the *MDDB
    write path*, reuse the index in the frontmatter as-is. The
    line-by-line body scan is replaced by a hash check (xxh64) — about
    9x faster at 1MB and 25x at 5MB.

    Small drawers (< 32 KB) are skipped because the fixed yaml parse
    cost is more expensive than the slow scan — callers fall back to
    ``_build_section_index`` directly. Algorithm-validation tests can
    force this path with ``min_bytes=0``.

    Returns ``None`` on failure (broken hash from external edits /
    missing index / format error) so the caller falls back to
    ``_build_section_index``.

    Returns:
        ``(sections_absolute, body_offset)`` or ``None``.
    """

    threshold = (
        _FAST_PATH_MIN_TEXT_BYTES if min_bytes is None else min_bytes
    )
    if len(text) < threshold:
        return None
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None
    try:
        meta, body = fm.parse(text)
    except Exception:
        return None
    sections_meta = meta.get(_ds._SECTIONS_META_KEY)
    hash_meta = meta.get(_ds._CONTENT_HASH_META_KEY)
    if not isinstance(sections_meta, list) or not isinstance(hash_meta, str):
        return None
    actual_hash = xxhash.xxh64(body.encode("utf-8")).hexdigest()
    if actual_hash != hash_meta:
        return None
    body_offset = len(text) - len(body)
    sections: dict[str, tuple[int, int]] = {}
    for entry in sections_meta:
        if not isinstance(entry, list) or len(entry) != 3:
            return None
        name, start, end = entry
        if (
            not isinstance(name, str)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or isinstance(start, bool)
            or isinstance(end, bool)
        ):
            return None
        if start < 0 or end < start or end > len(body):
            return None
        sections[name] = (body_offset + start, body_offset + end)
    return sections, body_offset


def _build_section_index(text: str) -> tuple[dict[str, tuple[int, int]], int]:
    """Extract a char-offset index of H2 sections from raw text.

    Returns:
        (sections, body_offset) — sections maps each H2 section id to
        (header start, just before the next H2) char offsets.
        body_offset is the position right after the frontmatter ends.
    """

    body_offset = 0
    if text.startswith("---\n") or text.startswith("---\r\n"):
        # Find the frontmatter end position
        # Look for the closing ---\n after the first newline
        first_nl = text.find("\n")
        if first_nl >= 0:
            search_from = first_nl + 1
            # End delimiter
            idx = text.find("\n---\n", search_from)
            if idx == -1:
                idx = text.find("\n---\r\n", search_from)
                if idx >= 0:
                    body_offset = idx + len("\n---\r\n")
            else:
                body_offset = idx + len("\n---\n")

    sections: dict[str, tuple[int, int]] = {}
    in_fence = False
    pos = body_offset
    n = len(text)
    line_start = pos
    current: tuple[str, int] | None = None
    while pos <= n:
        if pos == n or text[pos] == "\n":
            line = text[line_start:pos]
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
                        sections[current[0]] = (current[1], line_start)
                    current = (title, line_start)
            line_start = pos + 1
        if pos == n:
            break
        pos += 1
    if current is not None:
        sections[current[0]] = (current[1], n)
    return sections, body_offset


class DrawerCache:
    """One process = one DrawerCache. Thread-safe (RLock).

    Args:
        max_bytes: LRU memory ceiling (default 64MB). Beyond this, LRU evicts.
    """

    def __init__(self, *, max_bytes: int = _MAX_BYTES_DEFAULT) -> None:
        self._max_bytes = max_bytes
        self._cache: OrderedDict[Path, _DrawerEntry] = OrderedDict()
        self._lock = RLock()
        self._stats = DrawerCacheStats()
        # Stage Z.8 (2026-05-03) — per-process ULID. On flush, atomic_write
        # only to this process's delta path
        # (`<drawer>/_drawers/<stem>/delta-<session_lsn>.md`).
        # Other processes have other ULIDs, so conflicts are 0.
        self._session_lsn: str = str(new_ulid())

    def stats(self) -> DrawerCacheStats:
        with self._lock:
            return DrawerCacheStats(
                hits=self._stats.hits,
                misses=self._stats.misses,
                invalidations=self._stats.invalidations,
                evictions=self._stats.evictions,
                bytes_held=self._stats.bytes_held,
            )

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._stats.bytes_held = 0

    def invalidate(self, path: Path) -> None:
        """Explicit invalidation. Callers invoke this after write_section."""

        with self._lock:
            entry = self._cache.pop(path, None)
            if entry is not None:
                self._stats.invalidations += 1
                self._stats.bytes_held -= entry.size_bytes

    def _load(self, path: Path) -> _DrawerEntry | None:
        """Stage Z.8 (2026-05-03) — load main + all deltas merged into one *logical text* as the base.

        Looking only at the on-disk main means changes from other
        processes' deltas are invisible. Read views must be merged via
        ``merge_view`` to guarantee read-your-writes (across processes).

        - main absent + delta absent = None (the drawer itself is missing)
        - main absent + delta present = empty base + sections synthesized from deltas
        - main present + delta absent = on-disk main as-is (byte-equivalent to Z.7++)
        """

        main_exists = path.exists()
        deltas = _ds.list_delta_files(path)
        if not main_exists and not deltas:
            return None

        if main_exists and not deltas:
            # No deltas -> on-disk main as-is (byte-equivalent, original path preserved)
            # Stage U.9 (2026-05-08): mmap fast-path at 1MiB+
            try:
                stat = path.stat()
                text = _ds.read_drawer_text(path)
            except FileNotFoundError:
                return None
            return _DrawerEntry(
                text=text,
                mtime_ns=stat.st_mtime_ns,
                size_bytes=len(text.encode("utf-8")),
                delta_dir_mtime_ns=0,
            )

        # main + deltas (or deltas only) -> merge_view -> normalize via render_drawer
        merged = _ds.merge_view(path)
        # Standard _kind — merge_view brings the base meta as-is; if the surface is a drawer, keep _kind=drawer
        merged_meta = dict(merged.meta)
        merged_meta.setdefault("_kind", "drawer")
        new_contents = _ds.DrawerContents(
            meta=merged_meta, sections=merged.sections, prelude=merged.prelude
        )
        text = _ds.render_drawer(new_contents)
        try:
            main_mtime = path.stat().st_mtime_ns if main_exists else 0
        except FileNotFoundError:
            main_mtime = 0
        return _DrawerEntry(
            text=text,
            mtime_ns=main_mtime,
            size_bytes=len(text.encode("utf-8")),
            delta_dir_mtime_ns=_ds.delta_dir_mtime_ns(path),
        )

    def _evict_until_fit(self, incoming_bytes: int) -> None:
        """LRU evict to fit under the size cap. *Never* evicts dirty
        entries (to prevent silent data loss) — callers must flush
        first or raise the cap."""

        if not self._cache:
            return
        keys = list(self._cache.keys())
        i = 0
        while (
            self._stats.bytes_held + incoming_bytes > self._max_bytes
            and i < len(keys)
        ):
            k = keys[i]
            i += 1
            entry = self._cache.get(k)
            if entry is None or entry.dirty:
                continue
            self._cache.pop(k, None)
            self._stats.bytes_held -= entry.size_bytes
            self._stats.evictions += 1

    def _get_entry(self, path: Path) -> _DrawerEntry | None:
        """Internal: cache lookup + mtime check + load on miss.

        Stage Z.7++ (write-back): for dirty entries, the in-memory state
        is authoritative. External mtime changes are deferred to flush
        time as ConflictError — ignored here.
        """

        cached = self._cache.get(path)
        if cached is not None:
            if cached.dirty:
                # write-back: dirty buffers guarantee read-your-writes. No mtime check.
                self._cache.move_to_end(path)
                self._stats.hits += 1
                return cached
            # Stage Z.8 — both main mtime and delta dir mtime must match for a hit.
            try:
                main_mtime = path.stat().st_mtime_ns if path.exists() else 0
            except FileNotFoundError:
                main_mtime = 0
            delta_mtime = _ds.delta_dir_mtime_ns(path)
            # Both 0 = main absent + delta absent -> cache is meaningless, invalidate
            if main_mtime == 0 and delta_mtime == 0:
                self._cache.pop(path, None)
                self._stats.bytes_held -= cached.size_bytes
                self._stats.invalidations += 1
                self._stats.misses += 1
                return None
            if (
                main_mtime == cached.mtime_ns
                and delta_mtime == cached.delta_dir_mtime_ns
            ):
                # Real hit
                self._cache.move_to_end(path)
                self._stats.hits += 1
                return cached
            # mtime changed (main or delta dir) — invalidate + reload
            self._cache.pop(path, None)
            self._stats.bytes_held -= cached.size_bytes
            self._stats.invalidations += 1
        # Cold load
        entry = self._load(path)
        if entry is None:
            self._stats.misses += 1
            return None
        self._evict_until_fit(entry.size_bytes)
        self._cache[path] = entry
        self._stats.bytes_held += entry.size_bytes
        self._stats.misses += 1
        return entry

    def get_section(self, path: Path, section_id: str) -> str | None:
        """Return the section text. None on miss / missing section / missing file.

        The returned text *includes* the H2 header line. For dirty
        entries the overlay is checked first. When clean, it slices
        ``entry.text`` — byte-equivalent to a disk read.
        """

        with self._lock:
            entry = self._get_entry(path)
            if entry is None:
                return None
            return entry.logical_get_section(section_id)

    def get_full(self, path: Path) -> str | None:
        """Return the entire drawer (for small drawers). When dirty, render with the overlay applied."""

        with self._lock:
            entry = self._get_entry(path)
            if entry is None:
                return None
            if entry.dirty:
                return entry.render_logical()
            return entry.text

    def list_sections(self, path: Path) -> list[str]:
        """List of section IDs through the cache (logical state — base + overlay merged)."""

        with self._lock:
            entry = self._get_entry(path)
            if entry is None:
                return []
            return entry.logical_section_keys()

    def replace_entry(self, path: Path, new_text: str, new_mtime_ns: int) -> None:
        """Stage Z.7+ — after a write-through, replace the cache entry with the new text + mtime.

        Without a disk read, cache the *just-written* content directly.
        The next read/write hits without a cold load. The mtime is also
        updated to the new value, so the stale check passes.

        The eviction policy still applies — LRU evicts when max_bytes is
        exceeded.
        """

        # The section index is lazy — it's built on the next read. Write-only workloads pay nothing.
        new_entry = _DrawerEntry(
            text=new_text,
            mtime_ns=new_mtime_ns,
            size_bytes=len(new_text.encode("utf-8")),
        )
        with self._lock:
            old = self._cache.pop(path, None)
            if old is not None:
                self._stats.bytes_held -= old.size_bytes
            self._evict_until_fit(new_entry.size_bytes)
            self._cache[path] = new_entry
            self._stats.bytes_held += new_entry.size_bytes

    def peek_text_and_mtime(self, path: Path) -> tuple[str, int] | None:
        """Stage Z.7+ — for write-through cache. (text, mtime_ns) if present in cache.

        *Does not cold-load* — checks for a hit only. The mtime stale
        check is the caller's job (typically
        ``drawer_store.upsert_section`` compares stat under the lock).
        """

        with self._lock:
            cached = self._cache.get(path)
            if cached is None:
                return None
            return cached.text, cached.mtime_ns

    def get_meta(self, path: Path) -> dict[str, object] | None:
        """Return only the parsed frontmatter through the cache. overlay_meta applied.

        Stage Z.7++.2 — internal computed metadata (`_sections` /
        `_content_hash`) is not exposed to callers (same policy as the
        drawer _parse).
        """

        with self._lock:
            entry = self._get_entry(path)
            if entry is None:
                return None
            meta, _ = fm.parse(entry.text)
            meta = {
                k: v
                for k, v in meta.items()
                if k not in (_ds._SECTIONS_META_KEY, _ds._CONTENT_HASH_META_KEY)
            }
            if entry._overlay_meta:
                meta.update(entry._overlay_meta)
            return meta

    # ----- Stage Z.7++ — write-back (notepad mode) ---------------------------

    def _ensure_writable_entry(self, path: Path) -> _DrawerEntry:
        """Acquire the base entry for a write-back. Cold-load it or create an empty entry."""

        cached = self._cache.get(path)
        if cached is not None:
            self._cache.move_to_end(path)
            return cached
        # Cold-load from disk. If the file is missing, use an empty base entry (mtime_ns=0).
        loaded = self._load(path)
        if loaded is None:
            entry = _DrawerEntry(text="", mtime_ns=0, size_bytes=0)
        else:
            entry = loaded
        self._evict_until_fit(entry.size_bytes)
        self._cache[path] = entry
        self._stats.bytes_held += entry.size_bytes
        return entry

    def apply_section(
        self,
        path: Path,
        section_id: str,
        body: str,
        *,
        meta_update: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        """Stage Z.7++ / Z.7++.1 — record a section upsert in the overlay only. Doesn't touch disk and doesn't rebuild base text.

        N puts -> N overlay-dict updates (O(1) each). Only at flush is
        base + overlay rendered once. Removes the previous Z.7++'s N^2
        re-render cost.

        Reads (e.g. ``take_section``) check the overlay first ->
        guarantees read-your-writes.

        Stage II (2026-05-04): the return value includes
        ``drawer_size_bytes`` / ``section_count`` — callers (DrawerEngine
        / Database) post-process these as split-threshold signals.

        Stage R (2026-05-05): the return value also carries
        ``overwrote_existing`` (0/1). 1 if the same section_id already
        exists *logically* (base + overlay). The mddbtest cycle 35
        finding: when concurrent writes to the same section silently
        overwrite, the warning slot was missing. Callers (Database /
        AI) decide whether to alert based on the dict. We don't emit a
        WARN here — normal updates are common, so it would be noisy.
        """

        with self._lock:
            entry = self._ensure_writable_entry(path)
            existed = 1 if entry.has_logical_section(section_id) else 0
            diff = entry.apply_overlay_section(section_id, body, meta_update)
            self._stats.bytes_held += diff
            if diff > 0:
                self._evict_others_until_fit(path)
            return {
                "drawer_size_bytes": entry.logical_size_bytes(),
                "section_count": entry.logical_section_count(),
                "overwrote_existing": existed,
            }

    def apply_delete(self, path: Path, section_id: str) -> bool:
        """Stage Z.7++ / Z.7++.1 — mark a section deletion in the overlay.

        Returns:
            True if the section existed in the *logical* state and was
            marked for removal. False otherwise.
        """

        with self._lock:
            cached = self._cache.get(path)
            if cached is None:
                loaded = self._load(path)
                if loaded is None:
                    return False
                self._evict_until_fit(loaded.size_bytes)
                self._cache[path] = loaded
                self._stats.bytes_held += loaded.size_bytes
                cached = loaded
            else:
                self._cache.move_to_end(path)
            removed, delta = cached.apply_overlay_delete(section_id)
            self._stats.bytes_held += delta
            return removed

    def is_dirty(self, path: Path) -> bool:
        with self._lock:
            entry = self._cache.get(path)
            return entry is not None and entry.dirty

    def dirty_paths(self) -> list[Path]:
        """List of dirty drawer paths currently in the cache (LRU order)."""

        with self._lock:
            return [p for p, e in self._cache.items() if e.dirty]

    def flush(self, path: Path, *, fsync: bool = True) -> bool:
        """Stage Z.8 (2026-05-03) — atomic_write the overlay to *this process's delta file*.

        main is left untouched -> 0 mtime conflicts (each process only
        touches its own ULID delta path). Other processes' delta
        changes are surfaced on the next read by ``merge_view``.

        Delta file location:
        ``<main_parent>/_drawers/<main_stem>/delta-<session_lsn>.md``.
        The next flush from the same process *overwrites the same
        delta path* — accumulated changes from this process collapse
        into a single delta file (preventing file-count explosion).

        Args:
            path: the main drawer path.
            fsync: the fsync option for ``atomic_write_text``.

        Returns:
            True if a real disk write happened. False if not dirty
            (no-op).
        """

        with self._lock:
            entry = self._cache.get(path)
            if entry is None or not entry.dirty:
                return False

            # Decompose the overlay into delta sections + deleted_sections
            delta_sections: list[Section] = []
            deleted_section_ids: list[str] = []
            if entry._overlay:
                # Set of base keys — used to identify which _DELETED entries are meaningful (existed in base)
                base_keys = set(entry.sections().keys())
                for sid, val in entry._overlay.items():
                    if val is _DELETED:
                        if sid in base_keys:
                            deleted_section_ids.append(sid)
                    elif isinstance(val, str):
                        delta_sections.append(
                            Section(level=2, title=sid, content=val.rstrip("\n"))
                        )

            # Extract drawer_id — from the base meta (fall back to the path stem)
            drawer_id = path.stem
            if entry.text:
                try:
                    base_meta, _ = fm.parse(entry.text)
                    raw_id = base_meta.get("_drawer_id")
                    if isinstance(raw_id, str):
                        drawer_id = raw_id
                except Exception:  # noqa: BLE001
                    pass

            # Extract user meta — only the user-written traces (excluding system keys
            # _kind/_drawer_id/_sections/_content_hash/_lsn/_target/_deleted_sections).
            # Pass through delta's extra_meta so merge_view absorbs it into main meta
            # last-wins (preserves D7 vividness). Stage J (2026-05-04) regression fix.
            user_meta: dict[str, Any] = {}
            if entry._overlay_meta:
                for k, v in entry._overlay_meta.items():
                    if k in _ds.SYSTEM_META_KEYS:
                        continue
                    user_meta[k] = v

            # Zero changes (edge case): no-op.
            if not delta_sections and not deleted_section_ids and not user_meta:
                entry.dirty = False
                entry._overlay = None
                entry._overlay_meta = None
                entry._overlay_new_order = None
                self._stats.bytes_held -= entry._overlay_bytes
                entry._overlay_bytes = 0
                return False

            # atomic_write to this process's delta path — main is untouched
            _ds.write_delta(
                path,
                self._session_lsn,
                drawer_id=drawer_id,
                sections=delta_sections,
                deleted_sections=deleted_section_ids if deleted_section_ids else None,
                fsync=fsync,
                extra_meta=user_meta if user_meta else None,
            )

            # Our changes are out, so invalidate the entire cache entry — the next
            # read cold-loads main + all deltas (including ours). Guarantees
            # read-your-writes.
            self._cache.pop(path, None)
            self._stats.bytes_held -= entry.size_bytes + entry._overlay_bytes

        # Stage Z.8 — auto consolidate (outside the lock). In single-process
        # scenarios this immediately absorbs into main, preserving the
        # original behavior (cat main = latest).
        # Multi-process: serialized by the main FileLock. If delta unlink
        # fails, the next sleep cycle's MergeDrawerDeltasTask absorbs it
        # (idempotent).
        try:
            _ds.consolidate_drawer_deltas(path, fsync=fsync)
        except Exception:  # noqa: BLE001 — sleep absorbs consolidate failures
            pass
        return True

    def flush_all(
        self, *, fsync: bool = True, raise_on_conflict: bool = True
    ) -> tuple[int, list[ConflictError]]:
        """Flush all dirty drawers. Conflicts are collected or raised immediately.

        Args:
            fsync: the fsync option for each flush.
            raise_on_conflict: when True, raises immediately on the
                first conflict (flushes that already succeeded are
                kept). When False, attempts every flush and returns the
                conflict list.

        Returns:
            ``(flushed_count, conflicts)``.
        """

        conflicts: list[ConflictError] = []
        flushed = 0
        # Snapshot — dirty_paths can change during a flush
        for path in self.dirty_paths():
            try:
                if self.flush(path, fsync=fsync):
                    flushed += 1
            except ConflictError as e:
                if raise_on_conflict:
                    raise
                conflicts.append(e)
        return flushed, conflicts

    def _evict_others_until_fit(self, keep_path: Path) -> None:
        """LRU evict while preserving ``keep_path`` and any dirty
        entries — prevents evicting ourselves / other dirty buffers
        during a write-back (blocks silent data loss)."""

        keys = list(self._cache.keys())
        i = 0
        while (
            self._stats.bytes_held > self._max_bytes
            and i < len(keys)
        ):
            k = keys[i]
            i += 1
            if k == keep_path:
                continue
            entry = self._cache.get(k)
            if entry is None or entry.dirty:
                continue
            self._cache.pop(k, None)
            self._stats.bytes_held -= entry.size_bytes
            self._stats.evictions += 1


__all__ = ["DrawerCache", "DrawerCacheStats"]

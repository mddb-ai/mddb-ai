from __future__ import annotations

"""Stage Z.3 — Drawer Engine (thin wrapper integrating drawer_store + drawer_cache).

Vocabulary (R4 library terminology):
- **place** = ``put_section`` — put something into a section of a drawer
- **take** = ``take_section`` — retrieve a slice from a section (via RAM cache)
- **browse** = ``list_sections`` — list section IDs in a drawer
- **read whole** = ``take_drawer`` — small drawers as-is (recommended < 4KB)
- **save** = ``flush`` / ``flush_all`` — write dirty buffers to disk (Stage Z.7++)

Principle alignment:
- D1 no search — the engine only retrieves; the caller (AI) finds the path via Glob
- D2 no decisions — classification / splitting / **save timing** are the caller's
- R4 just laid out — only slices, no arbitrary format conversion
- T token equivalence — take_section text == byte-equivalent slice of disk read

Stage Z.7++ (2026-05-03, write-back notepad mode):
- ``put_section`` / ``delete_section`` *do not touch disk* — changes only
  go to the DrawerCache dirty buffer. Subsequent reads (take_section /
  take_drawer / list_sections) slice directly from the dirty entry,
  guaranteeing read-your-writes.
- Disk writes occur on ``flush(path)`` / ``flush_all()`` calls or via
  external safety nets (Database.close, sleep cycle's
  FlushDirtyDrawersTask).
- Multi-process safety: at flush time, if disk mtime differs from the
  base, ``ConflictError`` is raised.
- Crash responsibility: the AI (see flush call guidance in AGENT_GUIDE).
"""

from pathlib import Path
from typing import Any

from mddbai.brain.drawer_cache import DrawerCache
from mddbai.core.errors import ConflictError
from mddbai.storage import drawer_store


class DrawerEngine:
    """User-facing API on top of drawer_store + drawer_cache.

    Args:
        cache: Externally injected cache. If None, a default DrawerCache
            instance is created.
    """

    def __init__(self, *, cache: DrawerCache | None = None) -> None:
        self._cache = cache if cache is not None else DrawerCache()

    @property
    def cache(self) -> DrawerCache:
        return self._cache

    def put_section(
        self,
        path: Path,
        section_id: str,
        body: str,
        *,
        meta_update: dict[str, Any] | None = None,
        fsync: bool = True,  # noqa: ARG002 — applied at flush time
    ) -> dict[str, int]:
        """Place body content into one section of a drawer; replaces existing section.

        Stage Z.7++ (2026-05-03, write-back): no disk IO; only updates
        the cache dirty buffer. The ``fsync`` argument is kept for
        compatibility but ignored — actual fsync is applied at
        ``flush`` time.

        Stage II (2026-05-04): returns ``{"drawer_size_bytes": int,
        "section_count": int}`` so the caller can derive split-threshold
        signals.
        """

        return self._cache.apply_section(
            path, section_id, body, meta_update=meta_update
        )

    def take_section(
        self,
        path: Path,
        section_id: str,
        *,
        body_only: bool = False,
    ) -> str | None:
        """Return a slice of one section from a drawer.

        Args:
            path: Drawer .md path.
            section_id: H2 header text.
            body_only: If True, return content with the H2 header line
                stripped (R4 alignment: only when the AI explicitly
                requests it).

        Returns:
            Section text. None if the drawer or section is absent.
        """

        text = self._cache.get_section(path, section_id)
        if text is None:
            return None
        if not body_only:
            return text
        # Remove the first line (## section_id)
        nl = text.find("\n")
        if nl < 0:
            return ""
        return text[nl + 1 :].lstrip("\n")

    def take_drawer(self, path: Path) -> str | None:
        """Return the entire drawer (for small drawers)."""

        return self._cache.get_full(path)

    def list_sections(self, path: Path) -> list[str]:
        """List section IDs in a drawer (in order of appearance)."""

        return self._cache.list_sections(path)

    def delete_section(
        self, path: Path, section_id: str, *, fsync: bool = True  # noqa: ARG002
    ) -> bool:
        """Remove one section. Returns False if it does not exist.

        Stage Z.7++: write-back. No disk IO; updates the cache dirty
        buffer only. ``fsync`` is kept for compatibility and applied at
        flush time.
        """

        return self._cache.apply_delete(path, section_id)

    def flush(self, path: Path, *, fsync: bool = True) -> bool:
        """Stage Z.7++ — write the dirty buffer of one drawer to disk.

        Returns:
            True if a disk write actually occurred. False if not dirty.

        Raises:
            ConflictError: disk mtime differs from base mtime
                (external change).
        """

        return self._cache.flush(path, fsync=fsync)

    def has_dirty(self) -> bool:
        """Stage CC — check if any dirty entries exist (used by autosave to skip no-ops)."""

        return bool(self._cache.dirty_paths())

    def flush_all(
        self, *, fsync: bool = True, raise_on_conflict: bool = True
    ) -> tuple[int, list[ConflictError]]:
        """Flush all dirty drawers.

        Returns:
            ``(flushed_count, conflicts)``.
        """

        return self._cache.flush_all(
            fsync=fsync, raise_on_conflict=raise_on_conflict
        )

    def split_drawer(
        self,
        path: Path,
        plan: dict[str, list[str]],
        *,
        fsync: bool = True,
        extra_meta: dict[str, Any] | None = None,
    ) -> list[Path]:
        """Split a drawer into N new drawers per ``plan`` (Stage Z.7).

        Delegates to ``drawer_store.split_drawer`` and automatically
        invalidates caches (both the original and the new ones). The
        caller either constructs the plan directly or generates a
        time-fallback plan via ``plan_split_by_time`` and passes it in.

        Stage Z.7++ alignment: split operates on the *current disk
        state*, so any dirty buffers are *flushed first*. Raises
        ConflictError on conflict.
        """

        # Apply the dirty buffer before split — otherwise unflushed
        # changes would be missing from the split result.
        if self._cache.is_dirty(path):
            self._cache.flush(path, fsync=fsync)
        written = drawer_store.split_drawer(
            path, plan, fsync=fsync, extra_meta=extra_meta
        )
        self._cache.invalidate(path)
        for p in written:
            self._cache.invalidate(p)
        return written


__all__ = ["DrawerEngine"]

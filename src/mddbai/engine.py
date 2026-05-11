from __future__ import annotations

"""MDDB engine. Drawer model only. Removed record / WAL / LSM (Stage W).

Layout::

    <data_dir>/
      _palace.md              # palace root identity (optional)
      <table>/
        _palace/INDEX.md      # table palace structure (optional)
        <drawer>.md           # drawer file (composed of H2 sections)
        <sub>/<drawer>.md     # nested paths allowed
"""

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .storage.drawer_engine import DrawerEngine

from .core.clock import Clock, SystemClock
from .core.config import MddbConfig
from .core.errors import (
    InvalidKeyError,
    PalaceNotInitializedError,
)
from .core.logging import get_logger
from .core.types import TableName


def _is_skippable_dir_name(name: str) -> bool:
    """Skip system folders (underscore prefix) and hidden ones (dot prefix).

    Dot-prefix folders include user environment artifacts (.venv, .git, .idea,
    .pytest_cache, .ruff_cache) that should never be treated as data tables.
    """

    return name.startswith("_") or name.startswith(".")


class Database:
    """Drawer-only Database. Concurrency: single process, multiple threads, serialized via ``_lock``."""

    def __init__(
        self,
        data_dir: Path,
        *,
        config: MddbConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._config = config or MddbConfig(data_dir=self._dir)
        self._clock = clock or SystemClock()
        self._log = get_logger("mddbai.engine")
        self._lock = threading.RLock()

        # Drawer engine — lazy init
        self._drawer_engine: DrawerEngine | None = None

        # autosave (Word/Unreal pattern) — idle-timeout-based background flush
        self._last_activity_ns: int = 0
        self._autosave_stop = threading.Event()
        self._autosave_thread: threading.Thread | None = None

        # Log warn once per drawer when nearing the split threshold (per-drawer state)
        self._drawer_size_warned: set[Path] = set()

        self._start_autosave()

    # ---- public API ------------------------------------------------------

    def close(self) -> None:
        """Cleanup before process exit. Flushes drawer dirty buffers."""

        self._stop_autosave()

        with self._lock:
            if self._drawer_engine is not None:
                _, conflicts = self._drawer_engine.flush_all(
                    raise_on_conflict=False
                )
                for err in conflicts:
                    self._log.warning(
                        "drawer_flush_conflict_on_close",
                        path=err.context.get("path"),
                        expected_mtime_ns=err.context.get("expected_mtime_ns"),
                        actual_mtime_ns=err.context.get("actual_mtime_ns"),
                    )

    # ---- autosave thread --------------------------------------

    def _start_autosave(self) -> None:
        """Start the idle-timeout background flush thread."""

        if self._config.autosave_idle_seconds <= 0:
            return
        self._autosave_thread = threading.Thread(
            target=self._autosave_loop,
            name="mddbai-autosave",
            daemon=True,
        )
        self._autosave_thread.start()

    def _stop_autosave(self) -> None:
        self._autosave_stop.set()
        t = self._autosave_thread
        if t is not None and t.is_alive():
            t.join(timeout=5.0)
        self._autosave_thread = None

    def _mark_activity(self) -> None:
        """Call after mutations (put/put_section/delete). Resets the autosave timer."""

        self._last_activity_ns = time.monotonic_ns()

    def _autosave_loop(self) -> None:
        """Idle watch loop. Wakes every 1s and flushes once idle elapses."""

        idle_s = self._config.autosave_idle_seconds
        idle_ns = int(idle_s * 1_000_000_000)
        check_interval_s = min(1.0, idle_s / 3.0) if idle_s > 0 else 1.0

        while not self._autosave_stop.wait(check_interval_s):
            try:
                last = self._last_activity_ns
                if last == 0:
                    continue
                elapsed_ns = time.monotonic_ns() - last
                if elapsed_ns < idle_ns:
                    continue
                de = self._drawer_engine
                if de is None or not de.has_dirty():
                    continue
                self._last_activity_ns = 0
                with self._lock:
                    _, conflicts = de.flush_all(raise_on_conflict=False)
                self._log.info(
                    "autosave_flushed",
                    idle_seconds=idle_s,
                    conflicts=len(conflicts),
                )
                for err in conflicts:
                    self._log.warning(
                        "autosave_conflict",
                        path=err.context.get("path"),
                    )
            except Exception as exc:  # noqa: BLE001
                self._log.warning("autosave_loop_error", error=str(exc))

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @property
    def config(self) -> MddbConfig:
        return self._config

    def tables(self) -> list[TableName]:
        """List of table names currently present on disk."""

        names: set[str] = set()
        for entry in self._dir.iterdir():
            if entry.is_dir() and not _is_skippable_dir_name(entry.name):
                names.add(entry.name)
        return sorted(TableName(n) for n in names)

    # ---- palace strict guard (Stage AA.2) -------------------------------

    def _require_palace_init(self, table: str, op: str) -> None:
        """Block entry in strict mode. Raise if INDEX.md is missing."""

        import os as _os  # noqa: PLC0415

        if not self._config.require_palace_init:
            return
        if _os.environ.get("MDDB_PALACE_LENIENT") == "1":
            return
        from .brain.palace_init import has_index as _has_palace_index  # noqa: PLC0415

        if _has_palace_index(self._dir, table):
            return
        raise PalaceNotInitializedError(
            f"table {table!r} has no _palace/INDEX.md. "
            f"call db.init_palace({table!r}, ...) + db.confirm_init_palace({table!r}, ...) "
            f"first. (op={op})"
        )

    # ---- palace init (Stage AA, 2026-05-03) -----------------------------

    def has_palace_index(self, table: TableName | str) -> bool:
        """Whether ``<table>/_palace/INDEX.md`` exists."""

        from .brain.palace_init import has_index  # noqa: PLC0415

        return has_index(self._dir, str(table))

    def read_palace_index(
        self, table: TableName | str
    ) -> dict[str, Any] | None:
        """Return INDEX.md body + frontmatter. ``None`` if missing."""

        from .brain.palace_init import read_index  # noqa: PLC0415

        return read_index(self._dir, str(table))

    def init_palace(
        self,
        table: TableName | str,
        *,
        purpose: str,
        scale: str,
        axes: tuple[str, ...] | list[str],
        fallback: str = "auto_create",
    ) -> Any:
        """init steps 1+2: build a skeleton draft from the 4 answers. *Not written to disk*."""

        from .brain.palace_init import (  # noqa: PLC0415
            PalaceConfig,
            propose_skeleton,
        )

        cfg = PalaceConfig(
            purpose=purpose,
            scale=scale,
            axes=tuple(axes),
            fallback=fallback,
        )
        return propose_skeleton(cfg)

    def confirm_init_palace(
        self,
        table: TableName | str,
        draft: Any,
        folder_responsibilities: dict[str, str] | None = None,
    ) -> Path:
        """init step 3: AI fills in the responsibilities and finalizes. Writes INDEX.md to disk."""

        from .brain.palace_init import write_index  # noqa: PLC0415

        return write_index(self._dir, str(table), draft, folder_responsibilities)

    def update_palace_index(
        self,
        table: TableName | str,
        folder: str,
        responsibility: str,
    ) -> None:
        """Update INDEX.md after creating a new folder. Multi-writer safe."""

        from .brain.palace_init import update_index_responsibility  # noqa: PLC0415

        update_index_responsibility(
            self._dir, str(table), folder, responsibility
        )

    # ---- palace root identity (2026-05-06) ------------------------------

    def init_palace_root(
        self,
        *,
        purpose: str,
        scale: str,
        axes: tuple[str, ...] | list[str],
        fallback: str = "auto_create",
    ) -> Path:
        """Write palace root identity to data_dir/_palace.md. Atomic + multi-writer safe."""

        from .brain.palace_root import init_palace_root as _init_root  # noqa: PLC0415

        return _init_root(
            self._dir,
            purpose=purpose,
            scale=scale,
            axes=tuple(axes),
            fallback=fallback,
        )

    def has_palace_root(self) -> bool:
        """Whether data_dir/_palace.md exists."""

        from .brain.palace_root import has_palace_root as _has_root  # noqa: PLC0415

        return _has_root(self._dir)

    def read_palace_root(self) -> Any:
        """Return PalaceRootConfig from data_dir/_palace.md. None if missing."""

        from .brain.palace_root import read_palace_root as _read_root  # noqa: PLC0415

        return _read_root(self._dir)

    # ---- drawer model ---------------------------------------------------

    @property
    def drawer_engine(self) -> "DrawerEngine":
        """Lazily initialized DrawerEngine. One per process, sharing the cache."""

        if self._drawer_engine is None:
            from .storage.drawer_engine import DrawerEngine  # noqa: PLC0415

            self._drawer_engine = DrawerEngine()
        return self._drawer_engine

    def _drawer_path(
        self, table: TableName | str, drawer: str, *, validate: bool = True
    ) -> Path:
        """Compute the safe path of ``<data_dir>/<table>/<drawer>.md``."""

        if not drawer or drawer.startswith("/") or ".." in drawer.split("/"):
            raise InvalidKeyError(f"invalid drawer: {drawer!r}")
        t = str(table)
        if not t or "/" in t or t.startswith("_"):
            raise InvalidKeyError(f"invalid table: {t!r}")
        if validate:
            from mddbai.core.validation import validate_english_identifier  # noqa: PLC0415

            validate_english_identifier(t, kind="table")
            stem = drawer[:-3] if drawer.endswith(".md") else drawer
            for seg in stem.split("/"):
                if not seg:
                    raise InvalidKeyError(f"invalid drawer (empty segment): {drawer!r}")
                validate_english_identifier(seg, kind="drawer")
        rel = drawer if drawer.endswith(".md") else f"{drawer}.md"
        return self._dir / t / rel

    def put_section(
        self,
        table: TableName | str,
        drawer: str,
        section_id: str,
        body: str,
        *,
        meta_update: dict[str, Any] | None = None,
        fsync: bool = True,
    ) -> dict[str, Any]:
        """Place body into one section of a drawer (=put). Replaces existing section.

        The return value carries real-time signals about the split threshold:
        - ``drawer_size_bytes``: estimated logical size.
        - ``section_count``: logical section count.
        - ``split_threshold_bytes``: ``MddbConfig.drawer_split_bytes``.
        - ``size_pct_of_threshold``: 0.0 ~ N.
        - ``split_recommended``: ``True`` -> calling ``mddbai split-drawer`` is recommended.
        """

        self._require_palace_init(str(table), "put_section")
        path = self._drawer_path(table, drawer)
        merged_meta = {"_kind": "drawer", "_drawer_id": f"{table}/{drawer}"}
        if meta_update:
            merged_meta.update(meta_update)
        stat = self.drawer_engine.put_section(
            path, section_id, body, meta_update=merged_meta, fsync=fsync
        )
        self._mark_activity()

        threshold = max(1, self._config.drawer_split_bytes)
        warn_pct = self._config.drawer_split_warn_pct
        size_bytes = int(stat.get("drawer_size_bytes", 0))
        pct = size_bytes / threshold
        overwrote = int(stat.get("overwrote_existing", 0))
        out: dict[str, Any] = {
            "drawer_size_bytes": size_bytes,
            "section_count": int(stat.get("section_count", 0)),
            "split_threshold_bytes": threshold,
            "size_pct_of_threshold": round(pct, 4),
            "split_recommended": pct >= 1.0,
            "overwrote_existing": bool(overwrote),
        }
        if overwrote:
            try:
                rel = path.relative_to(self._dir).as_posix()
            except ValueError:
                rel = str(path)
            self._log.info(
                "section_overwrite",
                path=rel,
                table=str(table),
                drawer=str(drawer),
                section_id=section_id,
            )

        if pct >= warn_pct:
            with self._lock:
                first_time = path not in self._drawer_size_warned
                if first_time:
                    self._drawer_size_warned.add(path)
            if first_time:
                try:
                    rel = path.relative_to(self._dir).as_posix()
                except ValueError:
                    rel = str(path)
                drawer_id = drawer[:-3] if drawer.endswith(".md") else drawer
                self._log.warning(
                    "drawer_approaching_split_threshold",
                    path=rel,
                    drawer_size_bytes=size_bytes,
                    threshold_bytes=threshold,
                    pct=round(pct, 3),
                    section_count=out["section_count"],
                    split_command=(
                        f"mddbai split-drawer {self._dir} {table} {drawer_id} --by time"
                    ),
                )
        elif pct < warn_pct * 0.9:
            with self._lock:
                self._drawer_size_warned.discard(path)

        return out

    def put_section_meta(
        self,
        table: TableName | str,
        drawer: str,
        section_id: str,
        *,
        cue: list[str] | None = None,
        importance: float | None = None,
        related: list[str] | None = None,
        relations: list[dict[str, str]] | None = None,
        memory_zone: str | None = None,
        entity: list[str] | None = None,
        date: str | None = None,
        source: str | None = None,
        confidence: float | None = None,
        state: str | None = None,
        current_revision: str | None = None,
        supersedes: list[str] | None = None,
        aliases: list[str] | None = None,
        chosen_because: str | None = None,
        lang: str | None = None,
        merge: bool = True,
    ) -> dict[str, Any]:
        """Stage X (2026-05-06) — write navigation metadata for a section.

        Writes cue / importance / related / memory_zone into the drawer
        frontmatter at ``sections_meta[<section_id>]``. With ``merge=True``
        (default), keys whose new value is None keep the existing value.
        With ``merge=False``, the entry is replaced wholesale.

        When to call: right after ``put_section``, or as a separate
        metadata-write step. Metadata can be written even if the drawer
        has no section yet (if the drawer doesn't exist, only an empty
        entry is created).

        Returns:
            ``{"section_id": str, "cue": list, "importance": float|None,
            "related": list, "memory_zone": str|None}``.
        """

        from mddbai.codec.section_meta import (  # noqa: PLC0415
            Relation,
            SectionMetadata,
            SectionMetadataError,
            VALID_RELATION_KINDS,
            parse_sections_meta,
            serialize_sections_meta,
            validate_section_id,
        )

        validate_section_id(section_id)
        path = self._drawer_path(table, drawer)

        relations_objs: list[Relation] = []
        if relations:
            for entry in relations:
                if not isinstance(entry, dict):
                    raise SectionMetadataError(
                        f"relations entry must be a mapping, got {type(entry).__name__}"
                    )
                target = entry.get("target")
                kind = entry.get("kind")
                if not isinstance(target, str) or not target.strip():
                    raise SectionMetadataError(
                        "relations entry missing non-empty 'target'"
                    )
                if not isinstance(kind, str) or kind.strip() not in VALID_RELATION_KINDS:
                    raise SectionMetadataError(
                        f"relations.kind must be one of {VALID_RELATION_KINDS}, "
                        f"got {kind!r}"
                    )
                relations_objs.append(
                    Relation(target=target.strip(), kind=kind.strip())
                )

        new_meta = SectionMetadata(
            cue=list(cue) if cue else [],
            importance=importance,
            related=list(related) if related else [],
            relations=relations_objs,
            memory_zone=memory_zone,
            entity=list(entity) if entity else [],
            date=date,
            source=source,
            confidence=confidence,
            state=state,
            current_revision=current_revision,
            supersedes=list(supersedes) if supersedes else [],
            aliases=list(aliases) if aliases else [],
            chosen_because=chosen_because,
            lang=lang,
        )

        with self._lock:
            existing_full = self.drawer_engine.cache.get_meta(path) or {}
            try:
                existing = parse_sections_meta(dict(existing_full))
            except Exception:  # noqa: BLE001
                existing = {}
            prior = existing.get(section_id)
            if merge and prior is not None:
                # Fill only the empty fields from prior
                merged_meta = new_meta.merge(prior)
            else:
                merged_meta = new_meta
            existing[section_id] = merged_meta
            serialized = serialize_sections_meta(existing)

            # Even with no section body, metadata is still written. With no
            # body we don't create an empty placeholder — apply_section
            # can't do that, so we update the cache overlay_meta directly.
            entry = self.drawer_engine.cache._ensure_writable_entry(path)  # type: ignore[attr-defined]
            if entry._overlay_meta is None:  # type: ignore[attr-defined]
                entry._overlay_meta = {}  # type: ignore[attr-defined]
            entry._overlay_meta["sections_meta"] = serialized or {}  # type: ignore[attr-defined]
            entry.dirty = True
            self._mark_activity()

        return {
            "section_id": section_id,
            "cue": list(merged_meta.cue),
            "importance": merged_meta.importance,
            "related": list(merged_meta.related),
            "relations": [r.to_dict() for r in merged_meta.relations],
            "memory_zone": merged_meta.memory_zone,
            "entity": list(merged_meta.entity),
            "date": merged_meta.date,
            "source": merged_meta.source,
            "confidence": merged_meta.confidence,
            "state": merged_meta.state,
            "current_revision": merged_meta.current_revision,
            "supersedes": list(merged_meta.supersedes),
            "aliases": list(merged_meta.aliases),
            "chosen_because": merged_meta.chosen_because,
            "lang": merged_meta.lang,
        }

    def get_section_meta(
        self,
        table: TableName | str,
        drawer: str,
        section_id: str,
    ) -> dict[str, Any] | None:
        """Stage X (2026-05-06) — fetch a section's navigation metadata. None if missing."""

        from mddbai.codec.section_meta import parse_sections_meta  # noqa: PLC0415

        path = self._drawer_path(table, drawer)
        full = self.drawer_engine.cache.get_meta(path)
        if not full:
            return None
        try:
            sections_meta = parse_sections_meta(dict(full))
        except Exception:  # noqa: BLE001
            return None
        sm = sections_meta.get(section_id)
        if sm is None:
            return None
        return {
            "section_id": section_id,
            "cue": list(sm.cue),
            "importance": sm.importance,
            "related": list(sm.related),
            "relations": [r.to_dict() for r in sm.relations],
            "memory_zone": sm.memory_zone,
            "entity": list(sm.entity),
            "date": sm.date,
            "source": sm.source,
            "confidence": sm.confidence,
            "state": sm.state,
            "current_revision": sm.current_revision,
            "supersedes": list(sm.supersedes),
            "aliases": list(sm.aliases),
            "chosen_because": sm.chosen_because,
            "lang": sm.lang,
        }

    def take_section(
        self,
        table: TableName | str,
        drawer: str,
        section_id: str,
        *,
        body_only: bool = False,
    ) -> str | None:
        """Return the slice of one section in a drawer (=take). Routed via the RAM cache."""

        path = self._drawer_path(table, drawer)
        return self.drawer_engine.take_section(path, section_id, body_only=body_only)

    def take_drawer(self, table: TableName | str, drawer: str) -> str | None:
        """Return the entire drawer (=whole-file view). For small drawers."""

        path = self._drawer_path(table, drawer)
        return self.drawer_engine.take_drawer(path)

    def list_sections(self, table: TableName | str, drawer: str) -> list[str]:
        """List the section IDs inside a drawer (=browse)."""

        path = self._drawer_path(table, drawer)
        return self.drawer_engine.list_sections(path)

    def delete_section(
        self, table: TableName | str, drawer: str, section_id: str, *, fsync: bool = True
    ) -> bool:
        """Remove one section. Returns False if it doesn't exist."""

        path = self._drawer_path(table, drawer)
        result = self.drawer_engine.delete_section(path, section_id, fsync=fsync)
        if result:
            self._mark_activity()
        return result

    def rename_section(
        self,
        table: TableName | str,
        drawer: str,
        old_section_id: str,
        new_section_id: str,
        *,
        fsync: bool = True,
    ) -> bool:
        """Rename a section ID (preserving the body). The AI replaces a temporary slug with a meaningful one."""

        if not old_section_id or not new_section_id:
            return False
        path = self._drawer_path(table, drawer)
        de = self.drawer_engine
        body = de.take_section(path, old_section_id, body_only=True)
        if body is None:
            return False
        if old_section_id == new_section_id:
            return True
        # Conflict check — refuse if the target section already exists
        if de.take_section(path, new_section_id) is not None:
            return False
        de.put_section(path, new_section_id, body, fsync=False)
        de.delete_section(path, old_section_id, fsync=fsync)
        self._mark_activity()
        return True

    def rename_drawer(
        self,
        table: TableName | str,
        old_drawer: str,
        new_drawer: str,
        *,
        fsync: bool = True,
    ) -> bool:
        """Rename a drawer (whole body preserved). The AI replaces a temporary name with a meaningful slug."""

        import os  # noqa: PLC0415

        if not old_drawer or not new_drawer or old_drawer == new_drawer:
            return False

        old_path = self._drawer_path(table, old_drawer)
        new_path = self._drawer_path(table, new_drawer)
        if not old_path.exists():
            return False
        if new_path.exists():
            return False

        if (
            self._drawer_engine is not None
            and self._drawer_engine.cache.is_dirty(old_path)
        ):
            self._drawer_engine.flush(old_path, fsync=fsync)

        new_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old_path, new_path)
        try:
            text = new_path.read_text(encoding="utf-8")
            new_id = f"{table}/{new_drawer}"
            text = text.replace(
                f"_drawer_id: {table}/{old_drawer}\n",
                f"_drawer_id: {new_id}\n",
                1,
            )
            new_path.write_text(text, encoding="utf-8")
        except OSError:
            pass
        if self._drawer_engine is not None:
            self._drawer_engine.cache.invalidate(old_path)
            self._drawer_engine.cache.invalidate(new_path)
        self._mark_activity()
        return True

    def split_drawer(
        self,
        table: TableName | str,
        drawer: str,
        *,
        by: str = "time",
        new_drawers: dict[str, list[str]] | None = None,
        fsync: bool = True,
    ) -> list[str]:
        """Split a single drawer into N new drawers."""

        from mddbai.storage import drawer_store as _ds  # noqa: PLC0415

        path = self._drawer_path(table, drawer)
        if (
            self._drawer_engine is not None
            and self._drawer_engine.cache.is_dirty(path)
        ):
            self._drawer_engine.flush(path, fsync=fsync)
        if by == "time":
            if new_drawers is not None:
                raise ValueError("by='time' must not receive new_drawers")
            plan = _ds.plan_split_by_time(path)
        elif by == "manual":
            if not new_drawers:
                raise ValueError("by='manual' requires non-empty new_drawers")
            plan = dict(new_drawers)
        else:
            raise ValueError(f"unknown split strategy: {by!r}")

        extra: dict[str, Any] = {"_split_by": by}
        written = self.drawer_engine.split_drawer(
            path, plan, fsync=fsync, extra_meta=extra
        )
        out: list[str] = []
        for p in written:
            try:
                rel = p.relative_to(self._dir / str(table)).as_posix()
            except ValueError:
                rel = p.name
            if rel.endswith(".md"):
                rel = rel[:-3]
            out.append(rel)
        return out

    def list_cues(
        self,
        table: TableName | str,
        *,
        depth: int = 0,
        prefix: str | None = None,
    ) -> list[dict[str, object]]:
        """Dump per-drawer of cue traces written by the AI. No matching/embedding.

        Stage X (2026-05-06) — sections_meta integration:
        Each drawer's result dict carries a ``sections_meta`` key exposing
        per-section cue / importance / related / memory_zone. Body is 0
        bytes — a cold AI inspects this lightweight dump and decides
        which section to take.

        Returns:
            One dict per drawer::

                {
                    "drawer": "<rel>",
                    "frontmatter": {<user metadata, system keys excluded>},
                    "sections": ["sec1", "sec2", ...],
                    "sections_meta": {
                        "sec1": {
                            "cue": [...],
                            "importance": 0.8,
                            "related": [...],
                            "memory_zone": "hot",
                        },
                        ...
                    },
                }
        """

        import yaml  # noqa: PLC0415

        from mddbai.codec.section_meta import parse_sections_meta  # noqa: PLC0415

        t = str(table)
        if not t or "\\" in t or t.startswith("_"):
            raise InvalidKeyError(f"invalid table: {t!r}")
        from mddbai.core.validation import validate_english_identifier  # noqa: PLC0415

        validate_english_identifier(t, kind="table")
        root = self._dir / t
        if not root.exists():
            return []

        start = root
        if prefix:
            pfx_clean = prefix.rstrip("/")
            start = root / pfx_clean
            if not start.exists() or not start.is_dir():
                return []

        _SYSTEM_META = frozenset({
            "_kind", "_drawer_id", "_sections", "_content_hash",
            "_lsn", "_target", "_deleted_sections",
        })

        out: list[dict[str, object]] = []

        def _read_drawer_cues(p: Path, rel: str) -> dict[str, object] | None:
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                return None
            if not text.startswith("---"):
                return None
            parts = text.split("---", 2)
            if len(parts) < 3:
                return None
            try:
                fm = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                fm = {}
            if not isinstance(fm, dict):
                fm = {}
            # sections_meta is exposed separately, so pull it out of the frontmatter dump
            user_fm = {
                k: v
                for k, v in fm.items()
                if k not in _SYSTEM_META and k != "sections_meta"
            }
            try:
                sections_meta_parsed = parse_sections_meta(fm)
            except Exception:  # noqa: BLE001
                sections_meta_parsed = {}
            sections_meta_view: dict[str, dict[str, object]] = {}
            for sid, sm in sections_meta_parsed.items():
                d = sm.to_dict()
                if d:
                    sections_meta_view[sid] = d  # type: ignore[assignment]
            body = parts[2]
            sections: list[str] = []
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("## "):
                    sec_id = stripped[3:].strip()
                    if sec_id:
                        sections.append(sec_id)
            return {
                "drawer": rel,
                "frontmatter": user_fm,
                "sections": sections,
                "sections_meta": sections_meta_view,
            }

        def _walk_cues(cur: Path, cur_depth: int, parts_so_far: list[str]) -> None:
            try:
                entries = list(cur.iterdir())
            except OSError:
                return
            for ent in sorted(entries, key=lambda e: e.name):
                name = ent.name
                if _is_skippable_dir_name(name):
                    continue
                if ent.is_file() and name.endswith(".md"):
                    rel = "/".join(parts_so_far + [name[:-3]])
                    info = _read_drawer_cues(ent, rel)
                    if info is not None:
                        out.append(info)
                elif ent.is_dir():
                    if depth <= 0 or cur_depth + 1 < depth:
                        _walk_cues(ent, cur_depth + 1, parts_so_far + [name])

        _walk_cues(start, 0, [])
        return out

    def list_drawers(
        self,
        table: TableName | str,
        *,
        depth: int = 0,
        prefix: str | None = None,
        force_flat: bool = False,
    ) -> list[str]:
        """List drawers inside a table folder (.md files, with ``_`` prefix excluded).

        Stage X (2026-05-06) — auto depth clamp:
        If depth=0 (flat dump) is requested and the result exceeds
        ``flat_dump_threshold``, automatically downgrade to depth=1 +
        emit a logger warning. Prevents context explosion when a cold AI
        receives tens of thousands of drawers. ``force_flat=True`` keeps
        the flat dump (user-explicit).

        Returns:
            Sorted list of drawer names (relative to the table, with
            ``.md`` stripped). When depth > 0, folder entries are
            ``"<name>/"`` (with trailing slash).
        """

        t = str(table)
        if not t or "/" in t or t.startswith("_"):
            raise InvalidKeyError(f"invalid table: {t!r}")
        from mddbai.core.validation import validate_english_identifier  # noqa: PLC0415

        validate_english_identifier(t, kind="table")
        root = self._dir / t

        start = root
        if prefix:
            pfx_clean = prefix.rstrip("/")
            start = root / pfx_clean

        leaves, folders = self._collect_drawer_entries(start, depth)

        # Stage X — auto depth clamp (prevent flat dump explosion)
        threshold = self._config.flat_dump_threshold
        if (
            depth <= 0
            and not force_flat
            and len(leaves) + len(folders) > threshold
        ):
            self._log.warning(
                "list_drawers_auto_clamp",
                table=t,
                drawer_count=len(leaves),
                threshold=threshold,
                hint=(
                    f"use depth=1 or `mddbai list-drawers {self._dir} {t} --depth 1` "
                    "to traverse top folders, or pass --flat to force full dump"
                ),
            )
            leaves, folders = self._collect_drawer_entries(start, 1)

        result: list[str] = []
        result.extend(f"{f}/" for f in sorted(folders))
        result.extend(sorted(leaves))
        return sorted(result)

    def _collect_drawer_entries(
        self, start: Path, depth: int
    ) -> tuple[set[str], set[str]]:
        """Collect drawer leaf + folder entries inside ``start`` (respecting depth)."""

        leaves: set[str] = set()
        folders: set[str] = set()

        def _walk(cur: Path, cur_depth: int, parts: list[str]) -> None:
            try:
                entries = list(cur.iterdir())
            except OSError:
                return
            for ent in entries:
                name = ent.name
                if _is_skippable_dir_name(name):
                    continue
                if ent.is_file():
                    if not name.endswith(".md"):
                        continue
                    rel = "/".join(parts + [name[:-3]])
                    leaves.add(rel)
                elif ent.is_dir():
                    if depth <= 0 or cur_depth + 1 < depth:
                        _walk(ent, cur_depth + 1, parts + [name])
                    else:
                        folders.add("/".join(parts + [name]))

        _walk(start, 0, [])

        if self._drawer_engine is not None:
            for dp in self._drawer_engine.cache.dirty_paths():
                try:
                    rel = dp.relative_to(start).as_posix()
                except ValueError:
                    continue
                if any(p.startswith("_") for p in rel.split("/")):
                    continue
                if rel.endswith(".md"):
                    rel = rel[:-3]
                rel_parts = rel.split("/") if rel else []
                if depth <= 0 or len(rel_parts) <= depth:
                    leaves.add(rel)
                else:
                    folders.add("/".join(rel_parts[:depth]))

        return leaves, folders

    def summarize_drawers(
        self,
        table: TableName | str,
        *,
        prefix: str | None = None,
    ) -> dict[str, object]:
        """Stage X (2026-05-06) — navigation summary before a flat dump.

        For a cold AI to inspect explosion risk *before* calling
        ``list_drawers``. Returns per-folder drawer counts and a
        threshold-comparison signal.

        Returns:
            ``{
                "table": str,
                "total_drawers": int,
                "threshold": int,
                "would_explode": bool,
                "recommended_depth": int,
                "top_folders": [{"folder": str, "count": int}, ...],
                "root_drawers": int,
            }``
        """

        t = str(table)
        if not t or "/" in t or t.startswith("_"):
            raise InvalidKeyError(f"invalid table: {t!r}")
        from mddbai.core.validation import validate_english_identifier  # noqa: PLC0415

        validate_english_identifier(t, kind="table")
        root = self._dir / t

        start = root
        if prefix:
            start = root / prefix.rstrip("/")

        # Walk flat to gather every leaf
        leaves, _ = self._collect_drawer_entries(start, 0)
        total = len(leaves)
        threshold = self._config.flat_dump_threshold

        # Group by first segment
        per_folder: dict[str, int] = {}
        root_count = 0
        for rel in leaves:
            head, sep, _ = rel.partition("/")
            if sep:
                per_folder[head] = per_folder.get(head, 0) + 1
            else:
                root_count += 1

        top_folders = [
            {"folder": k, "count": v}
            for k, v in sorted(per_folder.items(), key=lambda x: (-x[1], x[0]))
        ]
        would_explode = total > threshold
        recommended_depth = 1 if would_explode else 0

        return {
            "table": t,
            "total_drawers": total,
            "threshold": threshold,
            "would_explode": would_explode,
            "recommended_depth": recommended_depth,
            "top_folders": top_folders,
            "root_drawers": root_count,
        }

    # ---- drawer registry (Stage X 2026-05-06) ---------------------------

    def registry_add_alias(self, alias: str, canonical: str) -> Path:
        """Register (or update) an alias in the drawer registry."""

        from mddbai.brain.registry import add_alias  # noqa: PLC0415

        return add_alias(self._dir, alias, canonical)

    def registry_remove_alias(self, alias: str) -> bool:
        """Remove an alias from the drawer registry."""

        from mddbai.brain.registry import remove_alias  # noqa: PLC0415

        return remove_alias(self._dir, alias)

    def registry_resolve(self, alias: str) -> str | None:
        """Resolve alias -> canonical drawer path."""

        from mddbai.brain.registry import resolve_alias  # noqa: PLC0415

        return resolve_alias(self._dir, alias)

    def registry_list_aliases(self) -> list[dict[str, str]]:
        """All registered aliases."""

        from mddbai.brain.registry import load_aliases  # noqa: PLC0415

        return [
            {"alias": a.alias, "canonical": a.canonical}
            for a in load_aliases(self._dir)
        ]

    def registry_overlaps(self) -> list[dict[str, Any]]:
        """Identify drawers whose stem appears in multiple locations (drift signal)."""

        from mddbai.brain.registry import detect_overlaps  # noqa: PLC0415

        reports = detect_overlaps(self)
        return [{"stem": r.stem, "locations": r.locations} for r in reports]

    def registry_suggest_reuse(
        self, intended_drawer: str, *, table: str | None = None
    ) -> list[str]:
        """Before placing a new drawer, suggest candidates with the same / partial stem."""

        from mddbai.brain.registry import suggest_reuse  # noqa: PLC0415

        return suggest_reuse(self, intended_drawer, table=table)

    # ---- related memory roads (Stage X 2026-05-06) ----------------------

    def collect_related(
        self, table: TableName | str, drawer: str
    ) -> list[dict[str, object]]:
        """Collect drawer-level + section-level related links for a drawer."""

        from mddbai.brain.related import collect_related_refs  # noqa: PLC0415

        refs = collect_related_refs(self, str(table), drawer)
        return [
            {
                "source": r.source,
                "target": r.target,
                "section_id": r.section_id,
                "exists": r.exists,
            }
            for r in refs
        ]

    def traverse_related(
        self,
        table: TableName | str,
        drawer: str,
        *,
        max_hops: int = 2,
        max_drawers: int = 50,
    ) -> list[dict[str, object]]:
        """BFS traversal — gather drawers up to ``max_hops`` away from the start drawer."""

        from mddbai.brain.related import traverse_related as _impl  # noqa: PLC0415

        hops = _impl(
            self,
            str(table),
            drawer,
            max_hops=max_hops,
            max_drawers=max_drawers,
        )
        return [
            {"drawer": h.drawer, "distance": h.distance, "via": list(h.via)}
            for h in hops
        ]

    def find_broken_related(
        self, table: TableName | str | None = None
    ) -> list[dict[str, object]]:
        """Identify ``related`` targets across all drawers that don't exist on disk."""

        from mddbai.brain.related import find_broken_links  # noqa: PLC0415

        refs = find_broken_links(self, str(table) if table else None)
        return [
            {
                "source": r.source,
                "target": r.target,
                "section_id": r.section_id,
            }
            for r in refs
        ]

    # ---- navigate (Stage X 2026-05-06) ----------------------------------

    def navigate(
        self,
        cue: str,
        *,
        max_routes: int = 5,
        max_tables: int = 5,
        max_drawers: int = 20,
        max_sections: int = 50,
        fallback_disabled: bool = False,
    ) -> dict[str, Any]:
        """Propose ``max_routes`` route candidates from a natural-language cue.

        No search engine. 0 bytes of drawer body are read. Only cue
        traces (palace / table summary / drawer name / section_id /
        sections_meta.cue / registry alias) are matched in a lightweight
        form.

        Args:
            cue: Single line of natural-language cue.
            max_routes / max_tables / max_drawers / max_sections:
                candidate caps.
            fallback_disabled: When True, disables drawer-level /
                palace-level fallback when there are no section-level
                candidates. Used by strict acceptance / harness
                verification. Forced True by default when the
                ``navigation_strict`` config is True.

        Returns:
            ``{
                "cue": str,
                "routes": [{"table", "drawer", "section", "reason", "signals"}],
                "warnings": [str, ...],
            }``
        """

        from .brain.navigation import navigate as _navigate  # noqa: PLC0415

        effective_fallback_disabled = (
            fallback_disabled
            or bool(getattr(self._config, "navigation_strict", False))
        )

        result = _navigate(
            self,
            cue,
            max_routes=max_routes,
            max_tables=max_tables,
            max_drawers=max_drawers,
            max_sections=max_sections,
            fallback_disabled=effective_fallback_disabled,
        )
        return result.to_dict()

    # ---- maintenance ---------------------------------------------------

    def flush(self, table: TableName | None = None) -> None:
        """Persist drawer dirty buffers to disk."""

        with self._lock:
            if self._drawer_engine is not None:
                if table is None:
                    self._drawer_engine.flush_all(raise_on_conflict=True)
                else:
                    table_root = self._dir / str(table)
                    cache = self._drawer_engine.cache
                    for path in cache.dirty_paths():
                        try:
                            path.relative_to(table_root)
                        except ValueError:
                            continue
                        cache.flush(path)

    # ---- delegation API -----------------------------------------------

    def write_summary(
        self,
        target: Path | str,
        content: str,
        *,
        authored_by: str = "ai",
        extra_meta: dict[str, Any] | None = None,
    ) -> Path:
        """Overwrite a folder's ``_summary.md`` wholesale with the AI's body."""

        from .brain.drawer_summary import write_summary as _impl  # noqa: PLC0415

        with self._lock:
            return _impl(
                self._dir,
                target,
                content,
                authored_by=authored_by,
                extra_meta=extra_meta,
            )

    def refresh_summaries(
        self,
        table: TableName | str,
        *,
        overwrite_ai: bool = False,
        sample_size: int = 5,
    ) -> list[Path]:
        """Stage X (2026-05-06) — auto-refresh stats in every folder's _summary.md within a table.

        Files marked ``_authored_by: ai`` are preserved unless
        ``overwrite_ai=False`` is overridden. Folders with zero drawers
        are skipped (to prevent orphan summaries).
        """

        from .brain.folder_summary import refresh_table_summaries  # noqa: PLC0415

        with self._lock:
            return refresh_table_summaries(
                self,
                str(table),
                overwrite_ai=overwrite_ai,
                sample_size=sample_size,
            )

    @property
    def now(self) -> int:
        """Current time (ns epoch)."""

        return self._clock.now_ns()


__all__ = ["Database"]

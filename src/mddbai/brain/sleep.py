from __future__ import annotations

"""Sleep Job — batch execution of maintenance tasks.

Task order: decay -> archive -> prune -> consolidation -> summary -> compaction.
Each task implements the ``SleepTask`` Protocol; ``SleepRunner`` runs it under
a file lock with the state file.
"""

import os
import time
import traceback
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from mddbai.codec.frontmatter import parse as fm_parse, render as fm_render
from mddbai.core.errors import LockTimeoutError
from mddbai.core.logging import get_logger
from mddbai.core.types import TableName
from mddbai.storage.atomic import atomic_write_text
from mddbai.storage.locks import FileLock

if TYPE_CHECKING:
    from mddbai.engine import Database

_log = get_logger("mddbai.brain.sleep")
SLEEP_LOCK = "_sleep.lock"
SLEEP_STATE = "_sleep_state.md"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SleepState:
    last_run_ns: int = 0
    checkpoints: dict[str, int] = field(default_factory=dict)
    # Stage N.3.1 — last homeostasis counts (surfaces silent reconcile).
    # doctor reads this dict and exposes it to the user.
    last_homeostasis: dict[str, int] = field(default_factory=dict)
    # Stage W: removed both the record-scan cache and the shard-entries cache
    # (scan / shard_index deprecated)


@dataclass
class SleepStepResult:
    task: str
    ok: bool
    metrics: dict[str, int | float] = field(default_factory=dict)
    error: str | None = None


@runtime_checkable
class SleepTask(Protocol):
    name: str

    def run(
        self, db: Database, *, now_ns: int, state: SleepState
    ) -> SleepStepResult: ...


# ---------------------------------------------------------------------------
# Standard tasks
# ---------------------------------------------------------------------------


def _list_tables(db: Database) -> list[TableName]:
    """Pull the currently known table list from ``Database.tables()``."""

    return db.tables()




@dataclass
class FlushDirtyDrawersTask:
    """Stage Z.7++ (2026-05-03) — drawer write-back safety net.

    Because ``Database.put_section`` operates in notepad mode (does not
    touch disk), if any dirty drawers are still pending at the start of
    the sleep cycle, we flush them *before any other task touches disk*
    (e.g. consolidation).

    Conflicts (mtime changed externally) are not raised — they are
    recorded as metrics. If sleep halted entirely on a partial conflict,
    the R4/D5 main task (consolidation) would also be blocked.
    Conflict resolution happens when the user explicitly calls
    ``db.flush()``.
    """

    name: str = "flush_dirty_drawers"

    def run(
        self, db: "Database", *, now_ns: int, state: SleepState
    ) -> SleepStepResult:
        engine = getattr(db, "_drawer_engine", None)
        if engine is None:
            return SleepStepResult(
                task=self.name, ok=True, metrics={"flushed": 0, "dirty": 0}
            )
        flushed, conflicts = engine.flush_all(raise_on_conflict=False)
        return SleepStepResult(
            task=self.name,
            ok=True,
            metrics={
                "flushed": flushed,
                "conflicts": len(conflicts),
            },
        )


@dataclass
class MergeDrawerDeltasTask:
    """Stage Z.8 (2026-05-03) — absorb per-process drawer delta files into main.

    Because ``DrawerCache.flush`` only atomic_writes to its own process's
    delta file (so multi-process conflicts are 0), accumulated delta
    files are merged back into main on every sleep cycle so that
    ``cat <drawer>.md`` does not show stale data.

    Behavior:
    - Walks each table's ``_drawers/<drawer_stem>/`` directory.
    - When a delta file is found, calls ``consolidate_drawer_deltas``
      (atomic absorb under the main FileLock + delta unlink).
    - If a new delta is added during merge due to a race, the next
      cycle absorbs it (idempotent).

    Runs right after ``FlushDirtyDrawersTask`` — the dirty buffers from
    this same cycle are merged in the same pass.
    """

    name: str = "merge_drawer_deltas"

    def run(
        self, db: "Database", *, now_ns: int, state: SleepState
    ) -> SleepStepResult:
        from mddbai.storage import drawer_store as _ds  # noqa: PLC0415

        data_dir = db.config.data_dir
        absorbed_total = 0
        merged_drawers = 0
        errors = 0
        for table in _list_tables(db):
            table_root = data_dir / str(table)
            delta_root = table_root / _ds.DELTA_DIR_NAME
            if not delta_root.exists():
                continue
            for drawer_subdir in delta_root.iterdir():
                if not drawer_subdir.is_dir():
                    continue
                main_path = table_root / f"{drawer_subdir.name}.md"
                try:
                    absorbed = _ds.consolidate_drawer_deltas(main_path)
                    if absorbed > 0:
                        absorbed_total += absorbed
                        merged_drawers += 1
                except Exception:  # noqa: BLE001
                    errors += 1
                    continue
        return SleepStepResult(
            task=self.name,
            ok=True,
            metrics={
                "merged_drawers": merged_drawers,
                "absorbed_deltas": absorbed_total,
                "errors": errors,
            },
        )




@dataclass
class BranchOverflowDetectorTask:
    """Branch overflow detection -> writes ``_attention.md`` (H.3, option B).

    Issues *warnings only*. The actual split is done by
    ``BranchTimeFallbackTask`` or by the calling AI invoking
    ``mddbai split-folder``.
    """

    threshold: int | None = None
    name: str = "branch_overflow_detect"

    def run(self, db: Database, *, now_ns: int, state: SleepState) -> SleepStepResult:
        from .branch_overflow import detect_overflow  # noqa: PLC0415

        reports = detect_overflow(db, threshold=self.threshold)
        return SleepStepResult(
            task=self.name,
            ok=True,
            metrics={
                "folders_overflowing": len(reports),
                "attention_files": sum(
                    1 for r in reports if r.attention_file is not None
                ),
            },
        )


@dataclass
class HomeostasisTask:
    """L.9 — automatic absorb / remove / reconcile of external .md (the sleep fsck).

    Runs immediately after ConsolidateSstTask. If external tools (AI
    Write, mv, rm, etc.) touch the records/ tree directly, the index
    catches up on the next sleep.
    """

    name: str = "homeostasis"

    def run(self, db: Database, *, now_ns: int, state: SleepState) -> SleepStepResult:
        from .homeostasis import homeostasis  # noqa: PLC0415

        absorbed_total = 0
        removed_total = 0
        reconciled_total = 0
        parse_fail_total = 0
        for table in _list_tables(db):
            try:
                report = homeostasis(db, table)
            except Exception:  # noqa: BLE001
                continue
            absorbed_total += report.cells_absorbed
            removed_total += report.cells_removed
            reconciled_total += report.cells_reconciled
            parse_fail_total += report.parse_failures
        # Stage N.3.1 — write the latest counts into sleep state (for doctor exposure)
        state.last_homeostasis = {
            "absorbed": absorbed_total,
            "removed": removed_total,
            "reconciled": reconciled_total,
            "parse_failures": parse_fail_total,
            "ts_ns": now_ns,
        }
        return SleepStepResult(
            task=self.name,
            ok=True,
            metrics={
                "cells_absorbed": absorbed_total,
                "cells_removed": removed_total,
                "cells_reconciled": reconciled_total,
                "parse_failures": parse_fail_total,
            },
        )






@dataclass
class LexiconExtractTask:
    """K.2.2 — extract raw utterances from ``_inbox/utterance/`` and write them as phrase nodes.

    Reads each raw file -> L1/L2/L3 extraction -> phrase node upsert ->
    marks the raw file's frontmatter with ``extracted: true``.
    """

    name: str = "lexicon_extract"

    def run(self, db: Database, *, now_ns: int, state: SleepState) -> SleepStepResult:
        from mddbai.brain import lexicon_extract as _ex  # noqa: PLC0415
        from mddbai.brain import lexicon_store as _store  # noqa: PLC0415
        from mddbai.brain import utterance as _utt  # noqa: PLC0415

        data_dir = db.config.data_dir
        # Ensure the seed dictionary exists
        _ex.ensure_signals_dictionary(data_dir)
        signals = _ex.load_signals(data_dir)

        pending = _utt.list_pending_utterances(data_dir)
        processed = 0
        new_nodes = 0
        reinforced = 0
        new_edges = 0

        for path in pending:
            try:
                fm, body = fm_parse(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if fm.get("extracted"):
                continue

            session_id = str(fm.get("session_id", "default"))
            captured_ns_raw = fm.get("captured_at_ns", now_ns)
            captured_ns = int(captured_ns_raw) if isinstance(captured_ns_raw, int) else now_ns

            # Pull out only the raw body text (frontmatter is already stripped)
            text = _strip_doc_marker(body)
            extracted = _ex.extract(text, signals=signals)

            if extracted.is_empty():
                fm["extracted"] = True
                fm["extracted_empty"] = True
                atomic_write_text(path, fm_render(fm, body))
                processed += 1
                continue

            intent_tags = sorted({s.tag for s in extracted.signals})

            # External feedback 2.2-a (2026-05-02): when the same phrase_key
            # appears multiple times in a single utterance, upsert only
            # once. This compresses token / node noise (e.g. "그 방식" /
            # "그방식" normalizing to the same key) while preserving the
            # "+1 per utterance" semantics on seen_count (D2-aligned —
            # the DB does not decide what is noise, it only dedups).
            dedup_keys: set[str] = set()

            for phrase in extracted.phrases:
                pk = _store.phrase_key(phrase)
                if pk in dedup_keys:
                    continue
                dedup_keys.add(pk)
                existed = _store.load_node(data_dir, phrase) is not None
                _store.upsert_phrase(
                    data_dir,
                    phrase,
                    ts_ns=captured_ns,
                    session_id=session_id,
                    intent_tags=intent_tags,
                )
                if existed:
                    reinforced += 1
                else:
                    new_nodes += 1

            # Deictic tokens also become nodes (with the deictic tag added to intent_tags)
            for deic in extracted.deictic:
                pk = _store.phrase_key(deic)
                if pk in dedup_keys:
                    continue
                dedup_keys.add(pk)
                existed = _store.load_node(data_dir, deic) is not None
                _store.upsert_phrase(
                    data_dir,
                    deic,
                    ts_ns=captured_ns,
                    session_id=session_id,
                    intent_tags=sorted({*intent_tags, "deictic"}),
                )
                if existed:
                    reinforced += 1
                else:
                    new_nodes += 1

            # Update the raw file's marker
            fm["extracted"] = True
            fm["extracted_at_ns"] = now_ns
            fm["phrases_extracted"] = len(extracted.phrases)
            fm["signals_extracted"] = [s.tag for s in extracted.signals]
            fm["deictic_extracted"] = list(extracted.deictic)
            atomic_write_text(path, fm_render(fm, body))
            processed += 1

        # Refresh the index
        if processed:
            _store.refresh_index(data_dir)

        return SleepStepResult(
            task=self.name,
            ok=True,
            metrics={
                "utterances_processed": processed,
                "new_phrase_nodes": new_nodes,
                "reinforced_nodes": reinforced,
                "new_edges": new_edges,
                "pending_total": len(pending),
            },
        )


def _strip_doc_marker(body: str) -> str:
    """Strip headers/comments from the utterance raw body and leave only the *utterance text*."""

    out_lines: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            out_lines.append(line)
            continue
        if s.startswith("#"):
            continue
        if s.startswith("<!--") or s.endswith("-->"):
            continue
        out_lines.append(line)
    return "\n".join(out_lines).strip()


@dataclass(slots=True)
class _UtteranceSnapshot:
    """Internal to LexiconReinforceTask — utterance snapshot (mutable fm)."""

    path: Path
    fm: dict[str, Any]
    body: str
    uid: str
    captured_ns: int
    text: str
    deictic: list[str]
    reinforced: bool


@dataclass
class LexiconReinforceTask:
    """K.5.1 — auto-add typed edges from the next-turn signal words to the *immediately previous output*.

    Processing flow:
    - Iterate over ``_inbox/utterance/*.md`` files marked ``extracted:
      true``, ascending by captured_at_ns.
    - Skip if the same utterance is already marked ``reinforced: true``.
    - If the *first 30 tokens* of the current utterance (N) contain a
      confirm signal word, add ``confirmed_with`` edges from each
      phrase of the previous utterance (N-1) to the current utterance
      id.
    - Reject signal words -> ``rejected_against``.
    - Each deictic token in the current utterance -> add ``referenced_by``
      edge to the previous utterance id.
    - When done, mark the utterance ``reinforced: true`` in the
      frontmatter to prevent reprocessing.

    Zero LLM calls. Morphology, regex, and a small dictionary only.
    """

    name: str = "lexicon_reinforce"
    head_token_count: int = 30

    def run(self, db: Database, *, now_ns: int, state: SleepState) -> SleepStepResult:
        from mddbai.brain import lexicon_extract as _ex  # noqa: PLC0415
        from mddbai.brain import lexicon_store as _store  # noqa: PLC0415
        from mddbai.brain import utterance as _utt  # noqa: PLC0415

        data_dir = db.config.data_dir
        signals = _ex.load_signals(data_dir)

        pending = _utt.list_pending_utterances(data_dir)
        if not pending:
            return SleepStepResult(
                task=self.name,
                ok=True,
                metrics={"processed": 0, "edges_added": 0, "pairs_examined": 0},
            )

        records: list[_UtteranceSnapshot] = []
        for path in pending:
            try:
                fm, body = fm_parse(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not fm.get("extracted"):
                continue
            captured_ns_raw = fm.get("captured_at_ns", 0)
            captured_ns = int(captured_ns_raw) if isinstance(captured_ns_raw, int) else 0
            uid = str(fm.get("id", path.stem))
            deictic_raw = fm.get("deictic_extracted", []) or []
            deictic_list = [str(d) for d in deictic_raw if isinstance(d, (str, int))]
            text = _strip_doc_marker(body)
            already_reinforced = bool(fm.get("reinforced", False))
            records.append(
                _UtteranceSnapshot(
                    path=path,
                    fm=fm,
                    body=body,
                    uid=uid,
                    captured_ns=captured_ns,
                    text=text,
                    deictic=deictic_list,
                    reinforced=already_reinforced,
                )
            )

        records.sort(key=lambda r: r.captured_ns)

        edges_added = 0
        processed = 0
        pairs_examined = 0

        for i in range(1, len(records)):
            cur = records[i]
            prev = records[i - 1]
            pairs_examined += 1
            if cur.reinforced:
                continue

            head_tokens = _ex.tokenize(cur.text)[: self.head_token_count]
            head_text = " ".join(head_tokens)

            confirm_hit = _signal_in_text(head_text, signals.get("confirm", ()))
            reject_hit = _signal_in_text(head_text, signals.get("reject", ()))
            decision_hit = _signal_in_text(head_text, signals.get("decision", ()))

            session_id = str(cur.fm.get("session_id", "default"))
            ts_iso = _iso_from_ns(cur.captured_ns)

            # confirm/decision -> confirmed_with edges
            if confirm_hit or decision_hit:
                for phrase in _phrases_in_text(prev.text):
                    _store.add_edge(
                        data_dir,
                        _store.LexiconEdge(
                            kind="confirmed_with",
                            source=_store.phrase_key(phrase),
                            target=cur.uid,
                            weight=1.0,
                            session_id=session_id,
                            ts_iso=ts_iso,
                        ),
                    )
                    edges_added += 1

            # reject -> rejected_against edges
            if reject_hit and not (confirm_hit or decision_hit):
                for phrase in _phrases_in_text(prev.text):
                    _store.add_edge(
                        data_dir,
                        _store.LexiconEdge(
                            kind="rejected_against",
                            source=_store.phrase_key(phrase),
                            target=cur.uid,
                            weight=1.0,
                            session_id=session_id,
                            ts_iso=ts_iso,
                        ),
                    )
                    edges_added += 1

            # deictic -> referenced_by edges (target is the previous utterance id)
            for deic in cur.deictic:
                _store.add_edge(
                    data_dir,
                    _store.LexiconEdge(
                        kind="referenced_by",
                        source=_store.phrase_key(deic),
                        target=prev.uid,
                        weight=1.0,
                        session_id=session_id,
                        ts_iso=ts_iso,
                    ),
                )
                edges_added += 1

            cur.fm["reinforced"] = True
            cur.fm["reinforced_at_ns"] = now_ns
            try:
                atomic_write_text(cur.path, fm_render(cur.fm, cur.body))
                processed += 1
            except OSError:
                continue

        return SleepStepResult(
            task=self.name,
            ok=True,
            metrics={
                "processed": processed,
                "edges_added": edges_added,
                "pairs_examined": pairs_examined,
            },
        )


def _signal_in_text(text: str, surfaces: Iterable[str]) -> bool:
    return any(s and s in text for s in surfaces)


def _phrases_in_text(text: str) -> list[str]:
    """LexiconReinforceTask re-extracts the phrases of the previous utterance."""

    from mddbai.brain import lexicon_extract as _ex  # noqa: PLC0415

    return _extract_n_grams(text, _ex)


def _extract_n_grams(text: str, _ex: Any) -> list[str]:
    extracted = _ex.extract(text)
    return list(extracted.phrases)


def _iso_from_ns(ns: int) -> str | None:
    if ns <= 0:
        return None
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat()


@dataclass
class LexiconPromoteTask:
    """Q.1.3 + external feedback 2.2-b (2026-05-02) — episodic -> semantic promotion.

    Gates (all must pass to promote):

    - **strength** >= ``min_strength`` (default 0.7) — usage-frequency based
    - **seen_count** >= ``min_seen`` (default 5) — blocks accidental appearances
    - **age** >= ``min_age_days`` (default 7) — blocks transient noise
    - **deictic_resolution_rate** >= ``deictic_min_rate`` (default 0.8) —
      External feedback 2.2-b. The fraction of ``referenced_by`` edges
      (where the node is the source) whose target utterance *actually*
      exists. With 0 edges the gate passes (non-deictic phrase nodes).

    Rationale: K.0.1's M3 (deictic resolution accuracy >= 80%) is
    enforced *locally at the promote stage*. Inaccurate nodes that get
    written as semantic anchors waste tokens in the next session and
    cause incorrect deictic resolution. Semantic is in the
    phagocytosis-protected region, so once a wrong node is written it
    cannot self-prune (Q.1.5 — phagocytosis only applies to episodic).
    Therefore the entry gate is the main control.

    Principle alignment:
    - D2: the gate is *numeric, mechanical work* — the DB does not
      interpret user vocabulary, it only checks whether retention
      conditions are met.
    - T (token resource): higher semantic anchor accuracy -> fewer
      recall reads in the next session.
    """

    name: str = "lexicon_promote"
    min_strength: float = 0.7
    min_seen: int = 5
    min_age_days: int = 7
    deictic_min_rate: float = 0.8

    def run(self, db: Database, *, now_ns: int, state: SleepState) -> SleepStepResult:
        from mddbai.brain import lexicon_store as _store  # noqa: PLC0415

        data_dir = db.config.data_dir

        # Load all episodic nodes + edges once
        all_nodes = _store.iter_all_nodes(data_dir)
        episodic_paths = [p for tier, p in all_nodes if tier == "episodic"]

        if not episodic_paths:
            return SleepStepResult(
                task=self.name,
                ok=True,
                metrics={
                    "examined": 0,
                    "promoted": 0,
                    "blocked_strength": 0,
                    "blocked_seen": 0,
                    "blocked_age": 0,
                    "blocked_deictic_rate": 0,
                },
            )

        edges = _store.load_edges(data_dir)
        # source key -> set of target uids of referenced_by edges
        ref_targets: dict[str, list[str]] = {}
        for e in edges:
            if e.kind != "referenced_by":
                continue
            ref_targets.setdefault(e.source, []).append(e.target)

        inbox = data_dir / "_inbox" / "utterance"

        def _utterance_exists(uid: str) -> bool:
            return (inbox / f"{uid}.md").is_file()

        min_age_ns = self.min_age_days * 86_400 * 1_000_000_000

        examined = 0
        promoted = 0
        blocked_strength = 0
        blocked_seen = 0
        blocked_age = 0
        blocked_deictic = 0

        for node_path in episodic_paths:
            try:
                fm, _ = fm_parse(node_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if fm.get("type") != "phrase":
                continue
            examined += 1

            try:
                strength = float(fm.get("strength", 0.0))
                seen_count = int(fm.get("seen_count", 0))
                first_seen_ns = int(fm.get("first_seen_ns", 0))
            except (TypeError, ValueError):
                continue

            surface = str(fm.get("surface", ""))
            if not surface:
                continue
            pkey = _store.phrase_key(surface)

            if strength < self.min_strength:
                blocked_strength += 1
                continue
            if seen_count < self.min_seen:
                blocked_seen += 1
                continue
            if first_seen_ns <= 0 or (now_ns - first_seen_ns) < min_age_ns:
                blocked_age += 1
                continue

            # External feedback 2.2-b: deictic resolution gate
            targets = ref_targets.get(pkey, [])
            if targets:
                resolved = sum(1 for t in targets if _utterance_exists(t))
                rate = resolved / len(targets)
                if rate < self.deictic_min_rate:
                    blocked_deictic += 1
                    continue

            new_path = _store.promote_to_semantic(data_dir, surface)
            if new_path is not None:
                promoted += 1

        # Refresh the index — if any promotion happened, refresh the surface->tier view
        if promoted:
            try:
                _store.refresh_index(data_dir)
            except OSError:
                pass

        return SleepStepResult(
            task=self.name,
            ok=True,
            metrics={
                "examined": examined,
                "promoted": promoted,
                "blocked_strength": blocked_strength,
                "blocked_seen": blocked_seen,
                "blocked_age": blocked_age,
                "blocked_deictic_rate": blocked_deictic,
            },
        )


@dataclass
class LexiconRetentionTask:
    """K.7.4 — apply retention to ``_inbox/utterance/``.

    Raw files marked ``extracted: true`` and older than
    ``retention_days`` are deleted. Unextracted files are preserved
    (the next sleep cycle will process them).
    """

    name: str = "lexicon_retention"
    retention_days: int = 30

    def run(self, db: Database, *, now_ns: int, state: SleepState) -> SleepStepResult:
        from mddbai.brain import utterance as _utt  # noqa: PLC0415

        deleted = _utt.apply_retention(
            db.config.data_dir,
            now_ns=now_ns,
            retention_days=self.retention_days,
            only_extracted=True,
        )
        return SleepStepResult(
            task=self.name,
            ok=True,
            metrics={"deleted": len(deleted)},
        )


def default_tasks(
    *,
    brain_auto: bool = False,
) -> list[SleepTask]:
    """Stage K.6 (2026-05-05) — opt-in brain automation.

    *Spatial-only tasks* are ON by default (D2 aligned):
    - FlushDirtyDrawersTask / MergeDrawerDeltasTask — drawer write-back safety net
    - HomeostasisTask — automatic absorption of external edits
    - BranchOverflowDetectorTask — folder overflow signaling (the AI
      decides meaning; the DB only emits the signal)

    The 4 *brain semantic-decision tasks* run only when
    ``brain_auto=True``:
    - LexiconExtractTask — auto phrase extraction from utterances
    - LexiconReinforceTask — auto edges to previous-turn phrases
    - LexiconPromoteTask — auto episodic -> semantic promotion
    - LexiconRetentionTask — auto utterance retention

    Stage X (2026-05-06): LinkDecayTask / LinkPruneTask removed (the
    record-id-based LinkStore is deprecated; replaced by a drawer-id
    based related graph).
    """

    base: list[SleepTask] = [
        FlushDirtyDrawersTask(),
        MergeDrawerDeltasTask(),
        HomeostasisTask(),
        BranchOverflowDetectorTask(),
    ]
    if brain_auto:
        base.extend(
            [
                LexiconExtractTask(),
                LexiconReinforceTask(),
                LexiconPromoteTask(),
                LexiconRetentionTask(),
            ]
        )
    return base


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class SleepRunner:
    """Sleep job runner with a file lock and a state file."""

    def __init__(
        self,
        db: Database,
        tasks: list[SleepTask],
        *,
        lock_path: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._db = db
        self._tasks = list(tasks)
        data_dir = db.config.data_dir
        self._lock_target = lock_path or (data_dir / SLEEP_LOCK)
        self._state_path = state_path or (data_dir / SLEEP_STATE)

    def is_running(self) -> bool:
        lock = FileLock(self._lock_target, timeout_s=0.0)
        try:
            lock.acquire_exclusive()
        except LockTimeoutError:
            return True
        else:
            lock.release()
            return False

    def run_once(self, *, now_ns: int) -> list[SleepStepResult]:
        lock = FileLock(self._lock_target, timeout_s=0.0)
        try:
            lock.acquire_exclusive()
        except LockTimeoutError:
            _log.info("sleep_skip_locked", target=str(self._lock_target))
            return []

        results: list[SleepStepResult] = []
        try:
            state = self._load_state()
            for task in self._tasks:
                step = self._run_one(task, now_ns=now_ns, state=state)
                results.append(step)
                state.checkpoints[task.name] = now_ns
            state.last_run_ns = now_ns
            self._save_state(state)
        finally:
            lock.release()
        return results

    # ---- internals ------------------------------------------------------

    def _run_one(
        self, task: SleepTask, *, now_ns: int, state: SleepState
    ) -> SleepStepResult:
        try:
            return task.run(self._db, now_ns=now_ns, state=state)
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc(limit=3)
            _log.error("sleep_task_failed", task=task.name, error=str(exc))
            return SleepStepResult(
                task=task.name,
                ok=False,
                metrics={},
                error=f"{exc}\n{tb}",
            )

    def _load_state(self) -> SleepState:
        if not self._state_path.exists():
            return SleepState()
        try:
            text = self._state_path.read_text(encoding="utf-8")
            meta, _ = fm_parse(text)
            if meta.get("_kind") != "sleep_state":
                return SleepState()
            checkpoints_raw = meta.get("checkpoints", {})
            last_h_raw = meta.get("last_homeostasis", {})
            last_homeostasis: dict[str, int] = {}
            if isinstance(last_h_raw, dict):
                for k, v in last_h_raw.items():
                    try:
                        last_homeostasis[str(k)] = int(v)
                    except (TypeError, ValueError):
                        continue
            return SleepState(
                last_run_ns=int(meta.get("last_run_ns", 0)),
                checkpoints={str(k): int(v) for k, v in dict(checkpoints_raw).items()},
                last_homeostasis=last_homeostasis,
            )
        except Exception:  # noqa: BLE001
            return SleepState()

    def _save_state(self, state: SleepState) -> None:
        meta: dict[str, Any] = {
            "_kind": "sleep_state",
            "last_run_ns": int(state.last_run_ns),
            "checkpoints": dict(state.checkpoints),
            # Stage N.3.1 — read by doctor for surfacing silent reconciles
            "last_homeostasis": dict(state.last_homeostasis),
        }
        body_lines = [
            "# Sleep State",
            "",
            f"last_run_ns: {state.last_run_ns}",
            "",
            "## Checkpoints\n",
        ]
        for name, ts in sorted(state.checkpoints.items()):
            body_lines.append(f"- {name}: {ts}")
        if state.last_homeostasis:
            body_lines.append("\n## Last Homeostasis\n")
            for k, v in sorted(state.last_homeostasis.items()):
                body_lines.append(f"- {k}: {v}")
        atomic_write_text(self._state_path, fm_render(meta, "\n".join(body_lines) + "\n"), fsync=False)


# ---------------------------------------------------------------------------
# Idle trigger
# ---------------------------------------------------------------------------


def should_run_sleep(
    db: Database, *, idle_threshold_s: float = 300.0, now_ns: int
) -> bool:
    """Decide whether to run sleep. After WAL removal, always returns True (the sleep cycle is safe)."""

    return True


def step_to_dict(step: SleepStepResult) -> dict[str, Any]:
    return asdict(step)


__all__ = [
    "BranchOverflowDetectorTask",
    "FlushDirtyDrawersTask",
    "MergeDrawerDeltasTask",
    "HomeostasisTask",
    "LexiconExtractTask",
    "LexiconPromoteTask",
    "LexiconReinforceTask",
    "LexiconRetentionTask",
    "SleepRunner",
    "SleepState",
    "SleepStepResult",
    "SleepTask",
    "default_tasks",
    "should_run_sleep",
    "step_to_dict",
]

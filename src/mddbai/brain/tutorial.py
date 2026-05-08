from __future__ import annotations

"""Tutorial entry-gate — ``<data_dir>/_brain/_tutorial_state.md``.

The *state-tracking slot* of the call-path gate. Separately from
``doctor --gate`` (which inspects the on-disk results), this checks the
*input procedure* — whether the caller (AI / script) is going through the
intended *conversational flow*.

Design principles:
- Aligned with D1 — zero LLM calls. Pure disk read/write.
- Aligned with D2 — semantic decisions are the AI's. This module tracks
  only the *entry procedure*.
- ``transactional_rmw`` makes it atomic + multi-writer safe.
- Idempotent — writing the same state twice is OK.
- The graduation marker (``passed_at``) is preserved once stamped, and
  refreshed on entry into a new stage.
- Recent N call statistics (``recent_calls``) — for doctor G4. Ring buffer.

Stage progression (``Step`` enum):
    palace      — palace_root (_palace.md) self-introduction required.
    kind        — kind declaration required.
    cue         — cue / entity declaration required.
    destination — candidate slot selection required.
    fit         — slot-fit (G2/G3) verification required.
    body        — body-format check (H2 enforced) required.
    save        — save + recall-check graduation exam.
    graduated   — graduated (declaration burden lifted; the essential
                  slots are always enforced).

This module handles only *state-file IO + step verification + call
recording*. CLI integration (write_cmd Steps 0~8 enforcement) lives in
``cli/main.py`` (T.2~T.4 slots).
"""

import datetime
import enum
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from mddbai.codec.frontmatter import parse as _fm_parse
from mddbai.codec.frontmatter import render as _fm_render
from mddbai.storage.transactional import transactional_rmw

TUTORIAL_STATE_REL = "_brain/_tutorial_state.md"
"""Relative path of the tutorial state file from data_dir."""

RECENT_CALLS_MAX = 50
"""Size of the recent-call ring buffer. doctor G4 measures the front-door ratio within this window."""

FRONT_DOOR_RATIO_MIN = 0.90
"""Lower bound for the front-door ratio. doctor G4 ERRORs below this value."""


class Step(str, enum.Enum):
    """Tutorial stages (one call = one stage)."""

    PALACE = "palace"
    KIND = "kind"
    CUE = "cue"
    DESTINATION = "destination"
    FIT = "fit"
    BODY = "body"
    SAVE = "save"
    GRADUATED = "graduated"


# Stage order — the forward direction (after graduation, restarts from destination).
_STEP_ORDER: tuple[Step, ...] = (
    Step.PALACE,
    Step.KIND,
    Step.CUE,
    Step.DESTINATION,
    Step.FIT,
    Step.BODY,
    Step.SAVE,
    Step.GRADUATED,
)

# Essential stages that are *always enforced* even after graduation.
ALWAYS_REQUIRED: frozenset[Step] = frozenset(
    {Step.DESTINATION, Step.FIT, Step.BODY, Step.SAVE}
)

# Declaration-burden stages that graduation lifts.
GRADUATION_EXEMPT: frozenset[Step] = frozenset({Step.KIND, Step.CUE})


@dataclass(frozen=True)
class CallRecord:
    """A single call record.

    Attributes:
        cmd: The command that was called (e.g. ``write`` / ``put-section``
            / ``read``).
        via_door: True if it came through the front door (``write`` /
            ``read``). False for legacy direct-save commands
            (put-section / ingest-document / split-drawer, etc.).
        ts: ISO8601 timestamp (UTC).
    """

    cmd: str
    via_door: bool
    ts: str


@dataclass(frozen=True)
class TutorialState:
    """Tutorial progression state.

    Attributes:
        step: The next stage that *must be performed*.
        passed_at: Graduation timestamp (None before graduation).
            Preserved once stamped.
        last_call_at: Timestamp of the last call.
        pending_kind: kind the user declared (set after Step.KIND passes).
        pending_cue: cue the user declared (set after Step.CUE passes).
        pending_entity: entity the user declared (set after Step.CUE
            passes; optional).
        recent_calls: Ring buffer of recent calls (up to
            RECENT_CALLS_MAX).
    """

    step: Step = Step.PALACE
    passed_at: str | None = None
    last_call_at: str | None = None
    pending_kind: str | None = None
    pending_cue: str | None = None
    pending_entity: str | None = None
    recent_calls: tuple[CallRecord, ...] = field(default_factory=tuple)

    def is_graduated(self) -> bool:
        """Whether the caller has graduated."""
        return self.passed_at is not None

    def front_door_ratio(self) -> float:
        """Front-door ratio across recent calls. 1.0 (vacuously true) if there are no calls."""
        if not self.recent_calls:
            return 1.0
        front = sum(1 for c in self.recent_calls if c.via_door)
        return front / len(self.recent_calls)

    def must_pass(self, step: Step) -> bool:
        """Whether the caller must go through this stage.

        Before graduation: every stage is enforced.
        After graduation: only ALWAYS_REQUIRED is enforced;
        GRADUATION_EXEMPT stages are lifted.
        """
        if not self.is_graduated():
            return True
        if step in ALWAYS_REQUIRED:
            return True
        return step not in GRADUATION_EXEMPT


def tutorial_state_path(data_dir: Path) -> Path:
    """Path to ``<data_dir>/_brain/_tutorial_state.md``."""
    return Path(data_dir) / TUTORIAL_STATE_REL


def has_state(data_dir: Path) -> bool:
    """Whether the tutorial state file exists."""
    return tutorial_state_path(data_dir).exists()


def read_state(data_dir: Path) -> TutorialState:
    """Restore the state from ``_tutorial_state.md``. Returns the initial state if the file is missing.

    If the file does not exist or its frontmatter is malformed, the
    initial state (Step.PALACE, not graduated, zero call history) is
    returned. This function never raises — callers must always receive
    a *usable* state to enter with.
    """
    p = tutorial_state_path(data_dir)
    if not p.exists():
        return TutorialState()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return TutorialState()
    try:
        front, _ = _fm_parse(text)
    except Exception:
        return TutorialState()

    step_raw = str(front.get("step", Step.PALACE.value))
    try:
        step = Step(step_raw)
    except ValueError:
        step = Step.PALACE

    passed_at = front.get("passed_at")
    passed_at_str: str | None = str(passed_at) if passed_at else None

    last_call_at = front.get("last_call_at")
    last_call_at_str: str | None = str(last_call_at) if last_call_at else None

    recent_raw = front.get("recent_calls", [])
    recent: list[CallRecord] = []
    if isinstance(recent_raw, list):
        for entry in recent_raw:
            if not isinstance(entry, dict):
                continue
            cmd = entry.get("cmd")
            via = entry.get("via_door")
            ts = entry.get("ts")
            if not isinstance(cmd, str) or not isinstance(ts, str):
                continue
            recent.append(
                CallRecord(cmd=cmd, via_door=bool(via), ts=ts)
            )

    pending_kind = front.get("pending_kind")
    pending_cue = front.get("pending_cue")
    pending_entity = front.get("pending_entity")

    return TutorialState(
        step=step,
        passed_at=passed_at_str,
        last_call_at=last_call_at_str,
        pending_kind=str(pending_kind) if pending_kind else None,
        pending_cue=str(pending_cue) if pending_cue else None,
        pending_entity=str(pending_entity) if pending_entity else None,
        recent_calls=tuple(recent[-RECENT_CALLS_MAX:]),
    )


def _render_state(state: TutorialState) -> str:
    """Render the state as frontmatter + a human-readable Markdown body."""
    front: dict[str, Any] = {
        "_kind": "tutorial_state",
        "_authored_by": "tutorial",
        "step": state.step.value,
        "graduated": state.is_graduated(),
    }
    if state.passed_at:
        front["passed_at"] = state.passed_at
    if state.last_call_at:
        front["last_call_at"] = state.last_call_at
    if state.pending_kind:
        front["pending_kind"] = state.pending_kind
    if state.pending_cue:
        front["pending_cue"] = state.pending_cue
    if state.pending_entity:
        front["pending_entity"] = state.pending_entity
    if state.recent_calls:
        front["recent_calls"] = [
            {"cmd": c.cmd, "via_door": c.via_door, "ts": c.ts}
            for c in state.recent_calls
        ]

    body_lines: list[str] = [
        "# Tutorial state",
        "",
        f"- current step: `{state.step.value}`",
        f"- graduated: {'yes' if state.is_graduated() else 'no'}",
        f"- front-door ratio (last {len(state.recent_calls)} calls): "
        f"{state.front_door_ratio():.2f}",
    ]
    if state.passed_at:
        body_lines.append(f"- graduated at: {state.passed_at}")
    body_lines.append("")
    body_lines.append(
        "This file is the SSOT for the mddbai tutorial entry-gate state. "
        "It checks the input procedure — whether the caller (AI / script) "
        "is going through the `mddbai write` / `mddbai read` front door. "
        "Do not edit by hand."
    )
    return _fm_render(front, "\n".join(body_lines) + "\n")


def write_state(data_dir: Path, state: TutorialState) -> Path:
    """Save the tutorial state atomically and multi-writer safely.

    No-op when identical to the previous state; otherwise atomically
    replaced.
    """
    p = tutorial_state_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_text = _render_state(state)

    def _mutator(existing_text: str) -> str:
        if existing_text == new_text:
            return existing_text
        return new_text

    transactional_rmw(p, _mutator)
    return p


def _now_iso() -> str:
    """Current time as ISO8601 (UTC)."""
    return datetime.datetime.now(datetime.UTC).isoformat()


def record_call(
    data_dir: Path,
    *,
    cmd: str,
    via_door: bool,
) -> TutorialState:
    """Record a call (front door / bypass). Returns the new state.

    Maintains the ring buffer at size RECENT_CALLS_MAX. Updates
    last_call_at.
    """
    state = read_state(data_dir)
    new_record = CallRecord(cmd=cmd, via_door=via_door, ts=_now_iso())
    new_calls = (*state.recent_calls, new_record)[-RECENT_CALLS_MAX:]
    new_state = replace(
        state,
        recent_calls=new_calls,
        last_call_at=new_record.ts,
    )
    write_state(data_dir, new_state)
    return new_state


def advance_step(
    data_dir: Path,
    *,
    next_step: Step,
    pending_kind: str | None = None,
    pending_cue: str | None = None,
    pending_entity: str | None = None,
) -> TutorialState:
    """Advance to the next stage. If pending_* arguments are provided, update those values.

    When next_step is Step.GRADUATED, passed_at is recorded automatically
    (graduation). If GRADUATED is written again on an already-graduated
    state, passed_at is preserved (idempotent).
    """
    state = read_state(data_dir)

    new_passed_at = state.passed_at
    if next_step is Step.GRADUATED and not state.is_graduated():
        new_passed_at = _now_iso()

    new_state = replace(
        state,
        step=next_step,
        passed_at=new_passed_at,
        pending_kind=pending_kind if pending_kind is not None else state.pending_kind,
        pending_cue=pending_cue if pending_cue is not None else state.pending_cue,
        pending_entity=(
            pending_entity if pending_entity is not None else state.pending_entity
        ),
    )
    write_state(data_dir, new_state)
    return new_state


def reset_pending(data_dir: Path) -> TutorialState:
    """Clear pending_kind / cue / entity to prepare for the next write.

    The graduation marker (passed_at) and call statistics (recent_calls)
    are preserved. After graduation, step is reset to DESTINATION;
    before graduation, it is reset to KIND.
    """
    state = read_state(data_dir)
    next_step = Step.DESTINATION if state.is_graduated() else Step.KIND
    new_state = replace(
        state,
        step=next_step,
        pending_kind=None,
        pending_cue=None,
        pending_entity=None,
    )
    write_state(data_dir, new_state)
    return new_state


def required_next_step(data_dir: Path) -> Step:
    """The *next* stage the current caller must go through.

    Returns Step.PALACE when the state file is absent.
    After graduation, GRADUATION_EXEMPT stages are skipped automatically.
    """
    state = read_state(data_dir)
    step = state.step
    if state.is_graduated():
        # After graduation, if we are still parked on KIND/CUE, advance to DESTINATION.
        while step in GRADUATION_EXEMPT:
            idx = _STEP_ORDER.index(step)
            if idx + 1 >= len(_STEP_ORDER):
                break
            step = _STEP_ORDER[idx + 1]
    return step


def front_door_ratio(data_dir: Path) -> float:
    """Front-door ratio of the current state. Called directly by doctor G4."""
    return read_state(data_dir).front_door_ratio()


def is_graduated(data_dir: Path) -> bool:
    """Whether the caller has graduated (mddbai write Step 8 passed at least once)."""
    return read_state(data_dir).is_graduated()

from __future__ import annotations

"""Stage AA (2026-05-03) — 4-step palace skeleton init dialog (table-scoped, opt-in).

Core insight (plans/01-core-insight.md):
- The memory palace is *space* provided by mddbai, not a *skeleton*.
- The AI builds the skeleton with its own hands on first entry, via the
  4-step ``init`` dialog.

See also: palace root identity (data_dir/_palace.md) uses
``init_palace_root`` from ``palace_root.py``. This module covers the
table-scoped fine-grained skeleton (opt-in).

Flow::

    db.init_palace(table, purpose, scale, axes, fallback)
        # returns a draft (not yet on disk)
        -> SkeletonDraft (folders / decision_rules)
    db.confirm_init_palace(table, draft, folder_responsibilities)
        -> INDEX.md written to disk + empty folder skeleton created
    db.update_palace_index(table, folder, responsibility)  # add new folder ad hoc
        -> INDEX.md update (transactional_rmw, multi-writer safe)

Design principles:
- D1 aligned — zero LLM calls. ``propose_skeleton`` is rule-based only.
- D2 aligned — mddbai only *proposes* a draft. Decision and confirmation
  are made by the AI via ``confirm_init_palace``.
- D6 aligned — INDEX.md updates are multi-writer safe via
  ``transactional_rmw``.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mddbai.codec.frontmatter import parse as _fm_parse
from mddbai.codec.frontmatter import render as _fm_render
from mddbai.core.validation import validate_english_identifier
from mddbai.storage.atomic import atomic_write_text
from mddbai.storage.transactional import transactional_rmw

INDEX_REL = "_palace/INDEX.md"
"""INDEX.md path relative to the table root."""

UNSORTED_PREFIX = "_unsorted/"
"""Prefix used to forcibly isolate keys when INDEX.md is absent."""

VALID_AXES = frozenset({"path", "time", "topic", "person", "free"})
VALID_SCALES = frozenset({"100", "1k", "10k", "100k", "1M+"})
VALID_FALLBACKS = frozenset({"auto_create", "unsorted"})


@dataclass(frozen=True)
class PalaceConfig:
    """Answers to the four init questions."""

    purpose: str
    scale: str
    axes: tuple[str, ...]
    fallback: str

    def __post_init__(self) -> None:
        if not self.purpose.strip():
            raise ValueError("purpose must not be empty")
        if self.scale not in VALID_SCALES:
            raise ValueError(
                f"scale must be one of {sorted(VALID_SCALES)} (got {self.scale!r})"
            )
        if not self.axes:
            raise ValueError("axes must contain at least 1 entry")
        unknown = set(self.axes) - VALID_AXES
        if unknown:
            raise ValueError(
                f"unknown axes: {sorted(unknown)}. "
                f"allowed: {sorted(VALID_AXES)}"
            )
        if self.fallback not in VALID_FALLBACKS:
            raise ValueError(
                f"fallback must be one of {sorted(VALID_FALLBACKS)} (got {self.fallback!r})"
            )


@dataclass(frozen=True)
class FolderProposal:
    name: str
    responsibility: str = ""


@dataclass(frozen=True)
class SkeletonDraft:
    config: PalaceConfig
    folders: tuple[FolderProposal, ...]
    decision_rules: tuple[str, ...]


def propose_skeleton(config: PalaceConfig) -> SkeletonDraft:
    """Derive a folder skeleton + decision rules from the four answers
    using rule-based logic.

    Zero LLM calls. Plain mapping.
    """

    folders: list[FolderProposal] = []
    rules: list[str] = []

    if "path" in config.axes:
        rules.append(
            "If the key contains a slash or '__', restore the tree as-is"
        )
    if "time" in config.axes:
        rules.append(
            "yyyy/mm/dd time nesting based on created_at (LayoutPolicy.TIME_NESTED)"
        )

    if {"topic", "free"} & set(config.axes):
        # Four common folders per domain — lowercase English enforced
        # (stage AA.4, global standard)
        folders.extend(
            [
                FolderProposal("code"),
                FolderProposal("docs"),
                FolderProposal("config"),
                FolderProposal("meta"),
            ]
        )
    if "person" in config.axes:
        folders.append(FolderProposal("people"))
    if not folders:
        # With only path / time, no semantic folders are needed — tree and
        # time alone are enough; provide one placeholder.
        folders.append(FolderProposal("general"))

    if config.fallback == "unsorted":
        rules.append(
            "If no INDEX.md folder matches, isolate under _unsorted/ and tidy later"
        )
    else:
        rules.append(
            "On a new pattern, create a folder ad hoc. INDEX.md update required"
        )

    return SkeletonDraft(
        config=config,
        folders=tuple(folders),
        decision_rules=tuple(rules),
    )


def index_path(data_dir: Path, table: str) -> Path:
    """``<data_dir>/<table>/_palace/INDEX.md`` path.

    Stage AA.4 (2026-05-03) — English identifiers enforced. ``_`` prefix
    is reserved for system use.
    """

    if not table or "/" in table or table.startswith("_"):
        raise ValueError(f"invalid table: {table!r}")
    validate_english_identifier(table, kind="table")
    return data_dir / table / INDEX_REL


def has_index(data_dir: Path, table: str) -> bool:
    """Whether INDEX.md exists on disk."""

    return index_path(data_dir, table).exists()


def read_index(data_dir: Path, table: str) -> dict[str, Any] | None:
    """Return the INDEX.md body + frontmatter. None if missing."""

    p = index_path(data_dir, table)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    front, body = _fm_parse(text)
    return {"front": front, "body": body, "path": p}


def write_index(
    data_dir: Path,
    table: str,
    draft: SkeletonDraft,
    folder_responsibilities: dict[str, str] | None = None,
) -> Path:
    """Write INDEX.md to disk and create the empty folder skeleton.
    The body of confirm_init.

    Stage AA.4 (2026-05-03) — all folder names go through the English
    identifier check. On Korean / uppercase / whitespace, raise
    ``InvalidKeyError`` with an English candidate suggestion.
    """

    folder_responsibilities = folder_responsibilities or {}

    # AA.4 — validate draft.folders as English. propose_skeleton already
    # writes its defaults in English, but drafts crafted by the AI should
    # still pass the safety net.
    for f in draft.folders:
        validate_english_identifier(f.name, kind="folder")

    folders_with_resp = [
        FolderProposal(
            name=f.name,
            responsibility=folder_responsibilities.get(f.name, f.responsibility),
        )
        for f in draft.folders
    ]

    front: dict[str, Any] = {
        "_kind": "palace_index",
        "purpose": draft.config.purpose,
        "scale": draft.config.scale,
        "axes": list(draft.config.axes),
        "fallback": draft.config.fallback,
    }
    body = _render_index_body(folders_with_resp, draft.decision_rules)
    text = _fm_render(front, body)

    p = index_path(data_dir, table)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, text)

    # Empty folder skeleton (semantic folders only — system folders such
    # as _palace/_unsorted are excluded)
    for f in folders_with_resp:
        (data_dir / table / f.name).mkdir(parents=True, exist_ok=True)

    return p


def update_index_responsibility(
    data_dir: Path,
    table: str,
    folder: str,
    responsibility: str,
) -> None:
    """Update the responsibility line of one folder in INDEX.md (create if
    missing). Multi-writer safe.

    Raises ``FileNotFoundError`` if INDEX.md does not exist.
    """

    p = index_path(data_dir, table)
    if not p.exists():
        raise FileNotFoundError(
            f"palace not initialized: {p}. Call db.init_palace + confirm first."
        )

    if not folder or folder.startswith("_") or "/" in folder:
        raise ValueError(f"invalid folder: {folder!r}")
    validate_english_identifier(folder, kind="folder")

    target_line = f"- {folder}/ — {responsibility}"

    def _mutate(text: str) -> str:
        front, body = _fm_parse(text)
        lines = body.splitlines()
        new_lines: list[str] = []
        replaced = False
        for line in lines:
            stripped = line.lstrip("- ").rstrip()
            if stripped.startswith(f"{folder}/ —") or stripped.startswith(
                f"{folder}/ "
            ):
                new_lines.append(target_line)
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            # Insert just before "## Decision rules". If absent, append.
            inserted = False
            buf: list[str] = []
            for line in new_lines:
                if not inserted and line.strip().startswith("## Decision rules"):
                    buf.append(target_line)
                    buf.append("")
                    inserted = True
                buf.append(line)
            if not inserted:
                buf.append("")
                buf.append(target_line)
            new_lines = buf
        new_body = "\n".join(new_lines)
        if not new_body.endswith("\n"):
            new_body += "\n"
        return _fm_render(front, new_body)

    transactional_rmw(p, _mutate)
    (data_dir / table / folder).mkdir(parents=True, exist_ok=True)


def isolate_key(key: str) -> str:
    """If INDEX.md is missing, force-isolate the key under
    ``_unsorted/<original-key>``."""

    if key.startswith(UNSORTED_PREFIX):
        return key
    return UNSORTED_PREFIX + key


def is_isolated(key: str) -> bool:
    return key.startswith(UNSORTED_PREFIX)


def _render_index_body(
    folders: list[FolderProposal], rules: tuple[str, ...]
) -> str:
    lines = [
        "# Palace INDEX",
        "",
        "This document is the *palace skeleton SSOT* of mddbai. It must be"
        " updated whenever a new folder is created.",
        "",
        "## Naming rules",
        "",
        "Folder names and file names must be **lowercase English**"
        " (`a-z 0-9 _ -`, length 1-64).",
        "No Korean, uppercase, whitespace, or special characters. MDDB targets a"
        " global open-source standard.",
        "Content (body, H2 titles, frontmatter values) may be multilingual —"
        " only the containers must be English.",
        "",
        "## Folders",
    ]
    for f in folders:
        resp = f.responsibility or "<no responsibility yet — fill in via confirm_init>"
        lines.append(f"- {f.name}/ — {resp}")
    lines.append("")
    lines.append("## Decision rules")
    for r in rules:
        lines.append(f"- {r}")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "INDEX_REL",
    "UNSORTED_PREFIX",
    "VALID_AXES",
    "VALID_FALLBACKS",
    "VALID_SCALES",
    "FolderProposal",
    "PalaceConfig",
    "SkeletonDraft",
    "has_index",
    "index_path",
    "is_isolated",
    "isolate_key",
    "propose_skeleton",
    "read_index",
    "update_index_responsibility",
    "write_index",
]

from __future__ import annotations

"""Primitive operation that atomically overwrites a drawer's _summary.md
with an AI-authored decision.

write_summary is extracted from delegation.py.
split_folder / move_record (which depended on lsm.manifest) are deprecated
along with it.
"""

from pathlib import Path
from typing import Any

from mddbai.codec.frontmatter import parse as fm_parse, render as fm_render
from mddbai.core.errors import MddbError
from mddbai.storage.atomic import atomic_write_text


SUMMARY_NAME = "_summary.md"
AUTHORED_BY_KEY = "_authored_by"
AUTHORED_BY_AI = "ai"
AUTHORED_BY_STATS = "stats"


class DrawerSummaryError(MddbError):
    """Failure writing a drawer's _summary.md."""


def _normalize_to_data_dir(data_dir: Path, target: Path | str) -> Path:
    """Normalize target to an absolute path inside data_dir.

    Args:
        data_dir: Database root.
        target: Absolute path, or path relative to data_dir.

    Raises:
        DrawerSummaryError: If target points outside data_dir.
    """
    base = Path(data_dir).resolve()
    raw = Path(target)
    if raw.is_absolute():
        candidate = raw.resolve()
    else:
        candidate = (base / raw).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise DrawerSummaryError(
            f"target escapes data_dir: {candidate} not under {base}",
            data_dir=str(base),
            target=str(target),
        ) from exc
    return candidate


def write_summary(
    data_dir: Path,
    target: Path | str,
    content: str,
    *,
    authored_by: str = AUTHORED_BY_AI,
    extra_meta: dict[str, Any] | None = None,
) -> Path:
    """Atomically overwrite a folder's _summary.md with AI-authored content.

    Args:
        data_dir: Database root.
        target: Path relative to data_dir (or absolute). Either the folder
            or the _summary.md file directly.
        content: New body (excluding frontmatter). Does not need to end with
            a newline.
        authored_by: Value for _authored_by. Defaults to "ai".
        extra_meta: Additional frontmatter keys. Existing keys such as
            _kind: folder_gist are preserved when possible.

    Returns:
        The path of the _summary.md that was actually written.

    Raises:
        DrawerSummaryError: If target is outside data_dir or invalid.
    """
    abs_path = _normalize_to_data_dir(data_dir, target)
    if abs_path.is_dir():
        summary_path = abs_path / SUMMARY_NAME
    elif abs_path.name == SUMMARY_NAME:
        summary_path = abs_path
    else:
        # May be a non-existent folder path -> treat as a folder
        summary_path = abs_path / SUMMARY_NAME

    summary_path.parent.mkdir(parents=True, exist_ok=True)

    # Preserve existing frontmatter (if any) + update _authored_by
    meta: dict[str, Any] = {}
    if summary_path.exists():
        try:
            old_text = summary_path.read_text(encoding="utf-8")
            old_meta, _ = fm_parse(old_text)
            meta = dict(old_meta)
        except Exception:  # noqa: BLE001
            meta = {}

    if "_kind" not in meta:
        meta["_kind"] = "folder_gist"
    meta[AUTHORED_BY_KEY] = authored_by
    if extra_meta:
        for k, v in extra_meta.items():
            if k.startswith("_") and k not in {AUTHORED_BY_KEY, "_kind"}:
                # Protect system keys
                continue
            meta[k] = v

    body = content if content.endswith("\n") else content + "\n"
    atomic_write_text(summary_path, fm_render(meta, body), fsync=False)
    return summary_path

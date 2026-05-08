from __future__ import annotations

"""Stage Z.9 (2026-05-03) — multi-process safe read-modify-write helper.

Background (Explore audit, 2026-05-03):
- The 70% loss observed in pre-Z.8 ``DrawerCache`` was caused by a pattern
  where another process could interleave during read-modify-write, making
  one side's changes vanish entirely.
- The same pattern existed in 6 more places: ``lexicon_store.add_edge`` /
  ``lexicon_store.upsert_phrase`` / ``homeostasis._save_state`` /
  ``links.LinkStore.save`` / ``folder_gist.write_gists`` /
  ``index_md.write_index``.

Solution (lock + atomic write pattern, like SVN/Perforce):
- Inside a FileLock, *actually* read from disk → mutate via the mutator
  function → atomic_write.
- Serializes when multiple processes touch the same file — guarantees zero
  loss.
- If the mutator returns *the same text*, no write happens (no-op
  optimization).

Typical usage::

    def add_my_thing(text: str) -> str:
        # text is empty string for a new file
        data = parse(text) if text else {"items": []}
        data["items"].append("new")
        return render(data)

    transactional_rmw(path, add_my_thing)
"""

from collections.abc import Callable
from pathlib import Path

from mddbai.storage.atomic import atomic_write_text
from mddbai.storage.locks import FileLock


def transactional_rmw(
    path: Path,
    mutator: Callable[[str], str],
    *,
    encoding: str = "utf-8",
    fsync: bool = True,
    timeout_s: float = 30.0,
) -> bool:
    """Perform a per-file read-modify-write atomically inside a lock.

    Args:
        path: Target file.
        mutator: ``text -> new_text``. ``text`` is the empty string for a new file.
        encoding: Read encoding (default utf-8).
        fsync: ``atomic_write_text`` fsync option.
        timeout_s: Lock acquisition timeout (seconds).

    Returns:
        True if a disk write actually occurred. False when the mutator
        returns the same text.

    Raises:
        LockTimeoutError: Failed to acquire the lock.
        OSError: Filesystem error.
        Any exception raised by ``mutator`` is propagated as-is.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(path, timeout_s=timeout_s):
        if path.exists():
            text = path.read_text(encoding=encoding)
        else:
            text = ""
        new_text = mutator(text)
        if new_text == text:
            return False
        atomic_write_text(path, new_text, fsync=fsync)
        return True


__all__ = ["transactional_rmw"]

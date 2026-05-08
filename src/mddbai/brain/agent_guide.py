from __future__ import annotations

"""``_AGENT_GUIDE.md`` — AI usage manual at the data root.

The point of `plans/02-master-plan.md` stage H.1: the real users of the
delegation API are LLMs like Claude Code / Codex CLI / Gemini CLI. They
do not import an SDK; they work with ``Glob / Grep / Read / Write / Bash``.
The usage of the delegation API therefore has to live *on disk itself*.

This module auto-generates ``<data_dir>/_AGENT_GUIDE.md``.

- If the file *does not* exist, :func:`ensure` writes the default guide
  atomically.
- If it *does* exist, it is not touched (so a manually edited guide is
  preserved).
- The frontmatter ``_authored_by: stats`` marks it as a *default*. When
  the AI replaces it with better content, the convention is to flip the
  field to ``_authored_by: ai``.
"""

from pathlib import Path

from mddbai.codec.frontmatter import render as fm_render
from mddbai.storage.atomic import atomic_write_text

AGENT_GUIDE_NAME = "_AGENT_GUIDE.md"


_DEFAULT_BODY = """\
# AGENT GUIDE — Hello, AI.

This directory is **your AI Memory Navigation Runtime**. You (Claude Code,
Codex CLI, Gemini CLI, Cursor, or any other capable AI) just started a
new session with no context. Some day a future you, in another fresh
session, has to land on the exact section using only *one ambiguous cue*
(time, color, sensation, association, deictic).

**Slogan**: `Map first. Lexical cue fallback only when necessary. Exact section read only.`

**Strict Retrieval principle**: never read a whole drawer. Only the
exact section makes it into the context.

MDDBAI is not a search engine, vector DB, or embedding retriever. It
exposes only the cue traces *the AI placed itself* (drawer slug /
section_id / sections_meta) as *space*. Semantic decisions, recall, and
reasoning are the AI's responsibility. There is no automatic extraction,
indexing, or brain refresh.

## Writing — the 3-call entry-point flow (aligned with T.2-T.4)

**The entry point is ``mddbai write``**. This flow gives the strongest
hooks for future recall.
The legacy commands (``put-section``, ``ingest-document``,
``split-drawer``, etc.) can still be called directly, but the resulting
recall cues / map hints will be weaker.

Multi-step write protocol (every save):

| Step | Who | Tool |
|---|---|---|
| 1. Write Intent Analysis | You — classify into memo/decision/rule/session/knowledge/source/todo/revision | (your own reasoning) |
| 2. Placement Decision | You — prefer reusing an existing drawer (avoid fragmentation) | `mddbai write` call 1 to inspect the map |
| 3. Semantic Structuring | You — internal section structure (Current / Why / Evidence / Related) | (you author it) |
| 4. Metadata Enrichment | You + MDDBAI — multi-representation cue + revision meta | `mddbai write --cue --entity --date --source --confidence` |
| 5. Relationship Linking | MDDBAI — related road for nearby sections/drawers | `mddbai link <a-ref> <b-ref>` (bidirectional) |
| 6. Future Recall Check | MDDBAI — navigate self-test right after writing | `--recall-check`, run automatically by `mddbai write --yes` |

### Call 1 — map dump (no destination)

```
mddbai write <data_dir> --kind <kind> --cue '<cue>'
```

Output: palace identity + existing folder list + cue-related candidate
drawers + options 1/2/3. Nothing is saved. rc 0.

### Call 2 — pick a destination + form guide (destination given, no --yes)

```
# Place into an existing drawer (option 1 — strongest recall, recommended)
mddbai write <data_dir> --kind <kind> --cue '<cue>' \\
  --ref <table>/<drawer>#<section>

# New drawer in an existing folder (option 2)
mddbai write <data_dir> --kind <kind> --cue '<cue>' \\
  --table <t> --new-drawer <slug>

# New drawer in a new folder (option 3)
mddbai write <data_dir> --kind <kind> --cue '<cue>' \\
  --new-table <t> --new-drawer <slug>
```

Output: placement review (drawer size / cue token overlap / kind fit) +
form guidance + the call-3 template. Nothing saved. rc 0.

The placement review is *advice only* — it never rejects. It tells you
"writing it like this makes future recall easier".

### Call 3 — actually save (--yes given)

```
mddbai write <data_dir> --kind <kind> --cue '<cue>' \\
  --ref <table>/<drawer>#<section> \\
  --body "body text" --yes
```

Save + recall-check. weak / miss results are still **saved** (no
rejection); only stderr warns. If the drawer reaches 40 MB,
``split_recommended`` is emitted.

### Legacy command guidance

``put-section`` / ``ingest-document`` / ``split-drawer`` /
``delete-section`` / ``rename-section`` / ``rename-drawer`` may still be
called directly, but stderr will print:

```
[hint] mddbai write is the recommended entry point for better future recall. The action will proceed.
```

Actions are not blocked — only the hint is shown.

## Reading — the 3-step entry-point flow

### Step 1 — try the exact section using a one-line cue

```
mddbai read <data_dir> "<single-line natural-language cue>"
```

1 candidate -> body returned. N candidates -> 0 bytes of body and a
prompt to use --select N or --ref. 0 candidates -> guidance to enrich
with the four dimensions (time / place / person / sensation).

### Step 2 — when ambiguous, use the map (parallel navigation)

```
mddbai map <data_dir> "<cue>"          # routes + drawers + related (zero body bytes)
mddbai map <data_dir> "<cue>" --json   # structured output
```

### Step 3 — once the exact section is fixed, read it directly

```
mddbai read <data_dir> "<cue>" --select N          # pick the Nth candidate
mddbai read <data_dir> "<cue>" --ref <table>/<drawer>#<section>  # direct reference
mddbai take <data_dir> <table> <drawer> <section>  # bypass navigate, slice directly
```

**Forbidden (raw-scan regression):**

| Surface | Enforcement | How |
|---|---|---|
| ``mddbai take <table> <drawer>`` (no section) | **Hard-blocked** | rc 2 + stderr under ``--strict`` |
| ``mddbai recall`` with ambiguous / missing cue | **Hard-blocked** | 0 bytes of body + rc 2 under ``--strict`` |
| ``mddbai read`` with N candidates | **Hard-blocked** | 0 bytes of body + rc 1, requires ``--select N`` or ``--ref`` |
| ``Read`` whole large .md inside ``.mddbai/`` | *Policy / guide* | Cannot block. You self-check. |
| ``Grep`` / ``Select-String`` over body text | *Policy / guide* | Same. |

## Drawer size management

- **>= 40 MB**: ``split_recommended`` signal. Run
  ``mddbai split-drawer``.
- **50 MB**: hard cap (blocked by ``doctor --gate``).
- Group the same topic into a single drawer — review existing drawers
  for one second before creating a new .md.

## Multi-step read protocol (6 steps)

| Step | Who | Tool |
|---|---|---|
| 1. Query Intent Analysis | You — fact/decision/reason/history/comparison/latest/action | (your own reasoning) |
| 2. Memory Route Planning | You — which table/drawer? *No body reads* | `_palace.md`, `_summary.md` |
| 3. Parallel Navigation | MDDBAI — routes + cues dump (zero drawer body) | `mddbai map <data> "<cue>"` |
| 4. Evidence Reading | MDDBAI — take only the exact section | `mddbai read` / `mddbai take` |
| 5. Meaning Reconstruction | You — combine active vs superseded | `--include-superseded`, `# conflict:` markers |
| 6. User Output Reconstruction | You — summary / timeline / comparison | (your own composition) |

## Principles — Loci 4 + Operations 4

**Loci 4 principles**: (1) Location — folder = room, one .md = drawer,
H2 = section. (2) Familiarity — well-trodden paths are fast.
(3) Vividness — write-time cue traces. (4) Order — time dimension.

**Four operating principles**:
1. **Aggregate.** Before creating a new .md, review existing drawers
   for one second. Same topic -> H2 sections inside one drawer.
2. **Notepad model.** put writes into a RAM buffer; flush hits the disk
   (like Word ctrl+s). 30-second idle is the safety net.
3. **Whole drawer in RAM, only the section in context.** take loads the
   whole drawer into RAM and slices the section by char-offset. No raw
   scans — use the mddbai recall / read / take flow.
4. **Cue form.** cue: time/place/person + key words (2-5 phrases).
   For bodies of 1 KB or more, split into ## H2 sections. Frontmatter
   cue tokens are written automatically.

## Your responsibilities (decided by the AI)

| Surface | Responsibility |
|---|---|
| Folder structure | Semantic folders / time folders — you decide |
| Cue traces | when/where/sense.touch/emotion/cue — you decide where to embed them |
| New vs existing drawer | Same topic in one drawer. 1-second review before a new .md |
| flush timing | Several sections, one flush |
| split timing | When you see split_recommended, you call it |

## What MDDB guarantees

- Precise slicing (whole drawer in RAM, only the section in context via
  char-offset)
- atomic write + per-drawer FileLock (ConflictError on contention)
- 40 MB split_recommended signal / 50 MB hard cap
- Token equivalence (disk read == cache hit, byte for byte)
- Notepad behavior (put -> RAM, flush -> disk)

## Helper tools

```
mddbai cues <data_dir>                        # flat dump of cue traces
mddbai navigate <data_dir> "<cue>"            # routes only
mddbai list-drawers <data_dir> <table>        # drawer list
mddbai list-sections <data_dir> <table> <drawer>  # section list
mddbai recall <data_dir> "<cue>" --strict     # exactly one section or 0 bytes
mddbai doctor <data_dir>                      # integrated checkup
mddbai link <data_dir> <a-ref> <b-ref>        # bidirectional link
mddbai flush <data_dir>                       # RAM -> disk
```

## Tree layout

- ``_palace.md`` — palace identity (purpose / scale / axes / fallback).
- ``_brain/`` — familiarity / lexicon / adjacency links.
- ``<table>/`` — a table. Drawers live as ``<table>/<drawer>.md``.

## gitignore hint (your call — mddbai never auto-edits)

```
# mddbai runtime / disk data
.mddbai/
```

If you want to commit it, leave it out. User's choice.

## Entry — palace_init

```
mddbai palace init <data_dir> \\
  --purpose "..." --scale "..." --axes "..." --fallback "..." \\
  --responsibilities-json '{"<table>": "one-line responsibility"}' \\
  --no-confirm
```

## Save only when the user *explicitly* asks

If there is no trigger, do not save. Just answer.

```
"take a note" / "save this" / "remember this" -> notes
"add a todo" / "TODO" -> tasks
"meeting summary" -> meetings
"let's go with this" / "decision" -> decisions
"quote" / "source" -> refs
```
"""


_GUIDE_VERSION = 21  # 2026-05-07: write 3-call flow (T.2~T.4) + read 4-dim boost (T.5) + legacy stderr guide (T.6) + AGENT_GUIDE refresh (T.7)


def default_text() -> str:
    """Default ``_AGENT_GUIDE.md`` text (frontmatter + body)."""

    meta = {
        "_kind": "agent_guide",
        "_authored_by": "stats",
        "version": _GUIDE_VERSION,
    }
    return fm_render(meta, _DEFAULT_BODY)


def _existing_authored_by(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        from mddbai.codec.frontmatter import parse as fm_parse  # noqa: PLC0415

        meta, _ = fm_parse(path.read_text(encoding="utf-8"))
        return str(meta.get("_authored_by", "")) or None
    except Exception:  # noqa: BLE001
        return None


def ensure(data_dir: Path, *, refresh: bool = False) -> Path:
    """Create ``<data_dir>/_AGENT_GUIDE.md`` with the default body if missing.

    Args:
        data_dir: Database root.
        refresh: When ``True``, overwrite an existing file with the default
            body if it is ``_authored_by: stats`` (or unknown). Files with
            ``_authored_by: ai`` are preserved (protecting guides edited
            by hand).

    Returns:
        File path (regardless of whether it was created or updated).
    """

    path = Path(data_dir) / AGENT_GUIDE_NAME
    if path.exists() and not refresh:
        return path
    if refresh and _existing_authored_by(path) == "ai":
        return path
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, default_text(), fsync=False)
    return path


__all__ = [
    "AGENT_GUIDE_NAME",
    "default_text",
    "ensure",
]

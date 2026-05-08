---
name: mddbai-consolidate
description: Standard flow for mddbai memory consolidation. A skill where the AI periodically tidies its own memory space to raise recall probability. Auto-loaded when the user says "consolidate memory" / "tidy up" / "consolidate" / "reinforce traces" / "clean stale memories" / "clean old stuff" / "merge duplicates" / "tune the memory palace", or when drawer accumulation has gone on for a while / recall failures repeat. Prefers archive / supersede / cue reinforcement over deletion. Destructive operations are plan-first — applied only after user approval.
---

# mddbai memory consolidation flow (consolidate)

No emojis. Paired with `mddbai-recall` / `mddbai-write`.

> **Slogan**: **Strengthen first. Archive next. Delete last.**
>
> Merge, reinforce cues (cue / alias / link), and move old memories to archive /
> supersede. Deletion is the last resort. Anything that looks important *requires
> user confirmation* before action.

---

## 1. Purpose (the slot of this skill)

mddbai is not a search store but *the AI's memory palace*. As time passes,
like the human brain, memories blur, duplicates pile up, and cues weaken. This
skill lets the AI *tend its memory space as if sleeping and consolidating*, so
the next cold AI can enter more easily.

Core:
- mddbai itself does not judge meaning. The *currently working AI* judges meaning (aligned with D2).
- This skill is not a *DB feature* but a *consolidation guide the AI follows*.
- Prefer archive / supersede over delete.
- If something looks important, never delete directly. Ask the user.
- Destructive operations are plan-first — propose only, apply after user approval.
- Every change is recorded in `_consolidation_log.md`.

---

## 2. When to use (triggers)

Auto-loaded in these slots:

- User utterances: "consolidate memory" / "tidy up" / "consolidate" / "reinforce traces" /
  "clean stale memories" / "clean old stuff" / "merge duplicates" / "tune the memory palace"
- Drawer accumulation signal: when `mddbai doctor` reports many orphans / duplicates / oversize
- Repeated recall failures: when traces of the same cue having entered multiple times without finding match are visible
- When a new decision arrives and conflicts with an old decision (supersede candidate emerges)

Only triggered when a `.mddbai/` folder exists in the current project cwd.

---

## 3. Core principles

| # | Principle | Description |
|---|---|---|
| C1 | DB does no semantic judgment | What is duplicate, what is contradictory — *the AI* decides |
| C2 | Strengthen > Archive > Delete | Strengthen first, then archive, deletion is the last resort |
| C3 | Plan first | Destructive operations need a proposal (`_consolidate_plan.md`) first |
| C4 | User approval gate | Risky operations are not applied until user approval |
| C5 | Important info safeguard | When important info is suspected, *always* ask the user |
| C6 | Trace everything | Every change is one row in `_consolidation_log.md` |
| C7 | Reversible by default | archive / supersede are reversible. Deletion is not |

---

## 4. Safe operations (auto-apply OK)

The following are safe to *apply directly by AI judgment*. No user approval.

| Operation | Meaning |
|---|---|
| `add_alias` | Add another name (alias) to the same section — strengthen recall cue |
| `add_cue` | Add word/phrase to the frontmatter `cue` key — reinforce write-time cues |
| `add_related_link` | Add link to a section that should recall together (Hebbian) |
| `update_summary` | Refresh the body of `_summary.md` / `_meta.md` / `_palace.md` |
| `fix_frontmatter_format` | Fix yaml format errors (indentation / quotes) |
| `report_broken_link` | *Report* a link whose target disappeared (do not auto-cut) |
| `report_duplicate` | *Report* sections that look semantically the same |
| `report_conflict` | *Report* two sections that contradict each other |
| `report_orphan` | *Report* isolated sections with weak cue / link |

Principle: *adding* or *reporting* is safe. *Removing / moving / merging / replacing* is risky.

---

## 5. Dangerous operations requiring approval

The following *must be in the plan and applied only after user approval*:

| Operation | Risk |
|---|---|
| `delete` | Permanently delete a section / drawer. Not reversible |
| `rename` | Replace drawer / section slug. May break cues |
| `move` | Move to a different drawer / folder. Moves the semantic location (Loci) |
| `merge` | Multiple sections -> one canonical. Old bodies are merged |
| `archive_confirm` | Move to `_archive/`. Excluded from main recall |
| `supersede_confirm` | Place `superseded_by` metadata and deactivate |
| `conflict_resolution` | Decide which side to adopt between two contradictory sections |
| `disable_important` | Deactivate important info |

Approval format: the user explicitly says "OK / apply / go" / row numbers ("only 1, 3, 5 OK").

---

## 6. Consolidation workflow (6 steps)

```
[1] Scan                         (mddbai doctor / cues / map, body 0)
    Light scan of the whole palace. Collect orphan / duplicate / oversize / conflict
    candidates. Body is not read (R1: only the exact section into context).

           |
           v
[2] Classify                     (AI, 0 tools)
    Classify each candidate into one of 9 action types:
    add_alias / add_cue / add_link / update_summary / fix_format /
    archive / supersede / merge / split

           |
           v
[3] Plan                         (Write `_consolidate_plan.md`)
    Author a proposed_actions table. Separate safe / dangerous. Dangerous gets
    a 1-line rationale. If important info is suspected, mark needs_user_check
    by §8 criteria.

           |
           v
[4] Auto-apply safe              (mddbai put-section etc., safe operations only)
    Auto-apply only the safe operations from §4. Record one row per change in
    `_consolidation_log.md`.

           |
           v
[5] User approval gate           (request user approval for §5 dangerous operations)
    Show the dangerous operation table to the user. The user approves per row.
    Only approved rows continue.

           |
           v
[6] Apply approved + log         (apply approved dangerous operations)
    Apply in order: archive / supersede / merge / delete. For each operation,
    one row in `_consolidation_log.md` (timestamp / action / target /
    reason / approval-id).
```

---

## 7. Action types (9)

| Type | Input | Output | Safe? |
|---|---|---|---|
| `add_alias` | drawer/section + alias string | adds frontmatter `alias` key | safe |
| `add_cue` | drawer/section + cue word | adds frontmatter `cue` key | safe |
| `add_link` | from / to section pair | adds frontmatter `related` key | safe |
| `update_summary` | `_summary.md` / `_meta.md` / `_palace.md` | rewrite body | safe |
| `fix_format` | frontmatter yaml error | format fix | safe |
| `archive` | drawer/section | move to `_archive/`, status=archived | dangerous |
| `supersede` | old section + new canonical section | `superseded_by` meta on the old | dangerous |
| `merge` | N sections + 1 canonical | merge body, supersede the old | dangerous |
| `split` | large drawer + semantic split plan | split into N new drawers | dangerous |

---

## 8. Important information deletion rule

If *any* of the following apply, treat as **important info**. Before archive /
supersede / delete / deactivation, ask the user (aligned with C5).

- project principle (principles in CLAUDE.md / `.claude/rules/`)
- architecture decision (rows in `plans/02-master-plan.md` decision table)
- external API usage (external dependencies / API key / endpoint)
- security / privacy related rules
- file structure principles (install-layout / folder slug policy)
- CLI integration principles
- global / language / encoding policy (Korean policy / no-emoji etc.)
- user-explicitly-decided content (items with "decided" / "confirmed" / "let's go with this" signals)
- core direction repeatedly mentioned (3+ times the user emphasized the same topic)
- records that other drawers link to (in-degree >= 1)

If any of the above triggers, mark `IMPORTANT` next to that row in the plan. The
approval format must also be *explicit* ("really archive it" / "really delete it") —
"OK" alone does not pass.

---

## 9. Status model

The frontmatter `status` value of a drawer / section is one of these 6.

| Value | Meaning | Visibility on recall |
|---|---|---|
| `active` | Currently valid main record | shown |
| `draft` | Incomplete / temporary | shown (with separate marker) |
| `superseded` | Replaced by a new canonical | hidden (follows superseded_by) |
| `archived` | Removed from main, retained | hidden (only via `mddbai recall --include-archived`) |
| `conflict` | Contradicts another, awaiting resolution | shown + warning |
| `needs_review` | AI requests review, awaiting user response | shown + warning |

The default is `active`. Whenever consolidate changes status, one row to log.

---

## 10. Logging rule

Every consolidate change accumulates one row in `.mddbai/_consolidation_log.md`.

Row format:

```
| timestamp | action | target | reason | approval | result |
```

- `timestamp`: ISO 8601 (`2026-05-08T14:30:00+09:00`)
- `action`: one of the 9 in §7
- `target`: `drawer/section` path
- `reason`: 1-line rationale (why this operation)
- `approval`: `auto` (safe operation) / `user:<approval-id>` (dangerous operation)
- `result`: `applied` / `skipped` / `error: <msg>`

`_consolidation_log.md` is the *source of truth for reverting* archive / supersede.
Never delete.

`_consolidate_plan.md` is the per-round planning document. When a round ends, move
to `_archive/plans/`.

---

## 11. Example consolidation plan

`.mddbai/_consolidate_plan.md` example:

```markdown
---
round: 2026-05-08-r1
scanned_at: 2026-05-08T14:00:00+09:00
total_drawers: 47
candidates: 12
---

# Consolidation Plan — 2026-05-08-r1

## Safe (auto-applied)

| # | action | target | reason |
|---|---|---|---|
| 1 | add_alias | decisions/install-policy#single-file | add alias "single installer" — frequent in user utterance |
| 2 | add_cue | decisions/install-policy#single-file | add "ps1" / "wheel embed" to cue |
| 3 | add_link | decisions/install-policy <-> decisions/install-layout | high co-recall frequency |
| 4 | update_summary | clusters/decisions/_meta.md | reflect last 3 decisions |
| 5 | fix_format | memos/2026-05-03 | yaml indentation error |

## Dangerous (needs approval)

| # | action | target | reason | important? |
|---|---|---|---|---|
| 6 | supersede | decisions/data-dir-old -> decisions/install-layout | data-dir decision replaced (after .mddbai single-location decision) | IMPORTANT (architecture decision) |
| 7 | merge | memos/idea-a + memos/idea-b -> memos/idea-merged | same idea written twice | normal |
| 8 | archive | tutorials/old-quickstart | old quickstart, replaced by new guide | normal |
| 9 | split | knowledge/big-doc (380KB) -> knowledge/big-doc-{a,b,c} | over the 256KB 80% threshold, 3-way semantic split | normal |

## Reports (no action, info only)

- orphan: 3 sections (memos/2026-04-15, memos/2026-04-18, memos/draft-x) — weak cues
- conflict: decisions/db-engine vs decisions/db-engine-v2 — must pick one
- broken_link: 5 cases (target disappeared)
```

---

## 12. Example user approval request

When showing the dangerous operation table to the user:

```
Memory consolidation round 2026-05-08-r1 — 4 dangerous operations awaiting approval

[6] supersede: decisions/data-dir-old -> decisions/install-layout
    rationale: data-dir decision replaced by the .mddbai single-location decision
    marker: IMPORTANT (architecture decision)
    to approve: "6 really supersede"

[7] merge: memos/idea-a + memos/idea-b -> memos/idea-merged
    rationale: same idea written twice
    to approve: "7 OK"

[8] archive: tutorials/old-quickstart
    rationale: old quickstart, replaced by the new guide
    to approve: "8 OK"

[9] split: knowledge/big-doc (380KB) -> 3-way split
    rationale: over the 256KB 80% threshold, by semantic units (intro / api / examples)
    to approve: "9 OK"

All OK: "all OK"
Partial: "6 really supersede, 7 OK, 8 OK" (skip 9)
Cancel: "cancel"
```

Apply only approved rows. After applying, one-line report:

```
Applied: 6 (supersede) / 7 (merge) / 8 (archive)
Held: 9 (split) — user did not approve
All recorded in _consolidation_log.md.
```

---

## Companion rules

- `.claude/skills/mddbai-recall/SKILL.md` — recall flow (consolidate makes recall easier)
- `.claude/skills/mddbai-write/SKILL.md` — write flow (consolidate tidies what was written)
- `.claude/rules/responsibility-split.md` — D1 / D2 alignment (semantic decisions = AI)
- `.claude/rules/identity.md` §3 — capability D (one-file consolidation + semantic folders)
- `plans/02-master-plan.md` decision table — one row for adding this skill (2026-05-08)

---

## One-line slogan

> **AI's strength = tending its own memory by its own hand.** mddbai gives the
> *space*, this skill gives the *flow*. Semantic judgment is the AI's, destruction
> authority is the user's.

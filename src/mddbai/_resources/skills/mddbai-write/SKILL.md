---
name: mddbai-write
description: Enforce the mddbai write standard flow. Auto-loaded when the user says "record this" / "save" / "remember" / "note" / "log it" / "leave a record", or when storing decisions / insights / meeting outcomes / learnings. 6-step flow (Write Intent -> Placement -> Semantic Structuring -> Metadata Enrichment -> Relationship Linking -> Future Recall Check) + 4 cues per save (cue in the user's language / alias / because / related) + 1-letter lang hint (ko/en auto-detect; on recall the cold AI does cross-language inference) + state/current_revision/supersedes metadata + put-section --recall-check self-recall simulation. Bidirectional adjacency edges are confirmed by a separate mddbai link call (write itself has no --link option).
---

# mddbai write standard flow (6 steps)

No emojis. Paired with the `mddbai-recall` skill.

> **Slogan**: **Map first. Lexical fallback only when necessary. Exact section read only.**
>
> The write step follows the slogan too — infer the placement *before* writing, run tools in parallel,
> and *leave precise traces* (so the next cold AI can recall the exact section).

---

## 1. Trigger conditions

Auto-loaded in these cases:

- User utterances: "record" / "save" / "remember" / "note" / "leave it" / "leave a record"
- Decision / insight / meeting outcome / learning / decision closing words ("decided" / "confirmed" / "let's go with this")
- User provides new info + "write this down" / "remember this"

Only triggered when a `.mddbai/` folder exists in the current project cwd.

---

## 2. The 6-step flow

```
[1] Write Intent Analysis      (AI, 0 tools)
    What are we writing? — memo / decision / rule / session / knowledge /
    source / todo / revision?
    e.g.  "Gangneung CEO decision" -> decision (potentially a revision)

           |
           v
[2] Placement Decision         (mddbai map / cues, parallel, body 0)
    Prefer reusing existing drawers (avoid fragmentation).
    One mddbai map .mddbai "<cue>" + (optional) cues / list-drawers in parallel
    -> within candidate drawers -> if a similar topic exists, add a new section to that drawer
    -> otherwise create a new drawer (verify alignment with palace.axes)

           |
           v
[3] Semantic Structuring       (AI, body authoring)
    Recommended in-section structure (not enforced, AI judges):
      ## <slug>
      ### Current      <- current conclusion
      ### Why          <- why this decision
      ### Evidence     <- evidence / data
      ### Related      <- references to adjacent sections
      ### Revision     <- evolution trace (optional)
      ### Confidence   <- 0.0~1.0 (optional)

    Revision steps share the slug + state separation:
      ## decision:r3   <!-- state: active -->
      ## decision:r2   <!-- state: superseded -->
      ## decision:r1   <!-- state: superseded -->

           |
           v
[4] Metadata Enrichment        (mddbai write / put-section, 4 traces + lang hint + extended meta)
    4 mandatory traces per save (entry points for the next cold AI):
      --cue "<one side, the user's language>"     <- forcing both KO and EN dropped (2026-05-08)
      --alias <abbreviation / nickname>           <- optional, only same-language aliases
      --because "<one line: why store this>"
      --related <table>/<drawer>#<section>        <- add adjacency refs at write time
                                                     (bidirectional confirmation in step 5)

    + 1-letter hint (automatic, user decision 2026-05-08):
      --lang auto         <- default. Auto-detects 'ko' / 'en' from body + cue.
      --lang ko|en|ja...  <- user explicit (overrides auto).
      --lang ""           <- do not store.

    On recall, the cold AI sees lang in the cues dump and does cross-language inference — no DB matching,
    the AI freely throws KO/EN to find it (aligned with D1/D2).

    Extended metadata (revision / tracking):
      --state active|superseded|deprecated
      --current-revision r3
      --supersedes r1 --supersedes r2
      --entity / --date / --source / --confidence
      --memory-zone hot|warm|cold|archive
      --importance 0~10

           |
           v
[5] Relationship Linking       (mddbai link, adjacency roads)
    Right after saving, add explicit roads to adjacent sections:
      mddbai link .mddbai \
        decisions/gangneung#ceo-q4 \
        decisions/architecture#supply-chain --bidir

    --bidir (default) — A <-> B bidirectional
    --unidir          — A -> B only

    Multiple links should be *parallel* (N calls in one message).

           |
           v
[6] Future Recall Check        (put-section --recall-check, self-recall simulation)
    write does its own self-test — right after writing, navigate self-test
    using the same cue. On weak/miss, prints a warning + reinforcement guidance to stderr.

    mddbai write enables this by default. Disable with --skip-recall-check.
    put-section also runs the same self-test when --recall-check is given.

    Result classes:
      ok      — the written ref is recalled at rank 1 by the cue
      weak    — appears among candidates but not rank 1 — alias / cue needs reinforcement
      miss    — 0 candidates — cue too weak, rewrite
```

---

## 3. The 4 traces (the *minimum* requirement of step 4)

When saving (`--yes`), **always** include these 4 + 1-letter lang hint:

### 3.1 `--cue` (one side, the user's language)

```bash
--cue "gangneung autumn meeting decision"   # use the user's language
# or, if the user spoke Korean, store the Korean text directly (lang=ko)
--cue "<korean phrase as-is>"
```

**Forcing both KO and EN cues is dropped** (user decision 2026-05-08). Use only the language the user gave. On recall, the cold AI sees `cue + lang` together in the `cues` dump and does cross-language inference — the DB does not match (aligned with D1/D2).

### 3.2 `--alias` (same-language aliases / abbreviations, optional)

```bash
--alias first-act --alias act1 --alias preparation
# (if the source language is Korean, store Korean aliases instead)
```

Reason: when the same meaning is also called by abbreviations / shorthand. Cross-language mapping is replaced by the `--lang` hint.

### 3.3 `--because` (why store this, 1 line)

```bash
--because "Step T demo result — aligned with Grep bypass blocking"
```

Reason: the next cold AI reads `chosen_because` and inherits the *previous AI's reasoning path* directly. A reasoning trace = a strong cold-start entry point.

### 3.4 `--related` (related section refs, strengthens adjacent recall)

```bash
--related decisions/architecture#strict-retrieval \
--related sessions/2026-05/8-tutorial-skill
```

Reason: Hebbian co-activation. On recall, when 1 candidate is found, follows `related` for spreading activation to adjacent sections.

> Note: `mddbai write` itself has no `--link` option. At write time
> use `--related` to add refs only, and confirm bidirectional roads
> in step 5 with the separate command `mddbai link <ref-A> <ref-B> --bidir`.

### 3.5 `--lang` (language hint, automatic)

```bash
--lang auto    # default — auto-detect ko / en from body + cue
--lang ko      # explicit Korean
--lang en      # explicit English
--lang ja      # Japanese / Chinese etc. — user explicit
--lang ""      # do not store
```

Reason: on recall, the cold AI reads the 1-letter lang in the cues dump and does cross-language inference. With one cue stored you can throw KO/EN freely to find it.

Stored frontmatter example (`.mddbai/decisions/lang-hint.md`):

```yaml
sections_meta:
  ko-decision:
    cue: [...]
    lang: ko          # 1-letter hint
  en-decision:
    cue: [...]
    lang: en
```

### Absolutely forbidden

```
[X] Just --yes with 0 of --cue / --because / --related
    -> 0 traces -> hard to enter on next recall -> principle violation
```

---

## 4. Extended metadata (revision / state)

When replacing an old decision with a new one — no whole-overwrite, preserve traces:

```bash
# Save r3 (new decision)
mddbai write .mddbai \
  --kind decision \
  --cue "q4 release schedule decision" \
  --because "Supply chain stable + marketing ready -> good timing" \
  --state active \
  --current-revision r3 \
  --supersedes r1 --supersedes r2 \
  --related decisions/architecture#supply-chain \
  --new-table decisions \
  --new-drawer gangneung-meeting \
  --new-section ceo-q4-decision:r3 \
  --body "## Current\nDecember launch ..." \
  --yes
```

The old r1 / r2 remain as separate sections with `--state superseded`. Recall by default shows only active. To see old items: `mddbai recall ... --include-superseded`.

If a section's related has a state conflict (active points to deprecated), recall stderr automatically prints `# conflict: ...` — semantic decisions are the AI's.

---

## 5. Parallel call steps (AI strength)

mddbai's single command is *already parallel* — `write` once bundles placement + semantic + metadata + related + recall-check 5 steps. The AI side should also *parallelize calls* when writing *multiple sections at once*:

```
Round 1 (in one message, when writing 1 large decision)
  └─ mddbai write .mddbai ... --yes
        (placement + semantic + metadata + related + recall-check at once)

Round 1 (in one message, when writing several sections at once)
  ├─ mddbai write .mddbai ... --new-section a --yes
  ├─ mddbai write .mddbai ... --new-section b --yes
  └─ mddbai write .mddbai ... --new-section c --yes
        (3 sections saved in parallel — safe via per-drawer FileLock)

Round 2 (in one message, links separately)
  ├─ mddbai link .mddbai a#x b#y --bidir
  ├─ mddbai link .mddbai a#x c#z --bidir
  └─ mddbai link .mddbai b#y c#z --bidir
```

For multiple sections in the same drawer, 1 call (multiple `put-section` + 1 `flush`) is faster. For different drawers, parallel calls are OK.

---

## 5.1 Leveraging AI strengths (3 beyond parallelism)

Three things in the write step that embeddings / vector DBs cannot catch up to. Turn each one on consciously per save.

### (a) Large context (1M tokens) — see all existing sections before writing

Old way: jump to a new drawer immediately -> fragmentation.

New way: before writing, run `mddbai map` + the candidate drawer's section list at once and *compare them all in large context*. "Is there already a similar section? Where should the new one go for adjacent recall?" — judge in one shot.

This is possible because of large context — humans do not (it's annoying, so they just create a new .md). Big difference in drawer reuse rate.

### (b) Metacognition (self-recall simulation + self-correction)

write does its own self-test:

```
save -> recall-check (step 6)
   ├─ ok    : the written location recalls at rank 1 by the cue -> done
   ├─ weak  : not rank 1 -> AI realizes "cue is weak" -> add alias -> rewrite
   └─ miss  : 0 candidates -> AI realizes "cue too weak" -> enrich cue/alias -> retry
```

Algorithms cannot tell their own write was weak. The AI does a self-recall right after writing and corrects on weakness. This is the central core of mddbai body §8 (Write = Future Recall Preparation).


### (c) Semantic abstraction (user language + lang hint, cross-language inference is the AI's responsibility on recall)

**Use only the language the user gave** (decided 2026-05-08). Korean utterance -> Korean cue, English utterance -> English cue. Forcing both is dropped — the trace is just a 1-letter `lang` hint, lightweight.

Write the same section as multiple *meaning facets*, but within the same language:

```bash
# Korean utterance slot (Korean cues stored as-is; --lang auto detects ko)
--cue "<korean cue 1>" \
--cue "<korean cue 2>" \
--alias "<korean alias 1>" --alias "<korean alias 2>"
# (--lang defaults to 'auto' -> detected as 'ko')
```

On recall the cold AI gets the `cues` dump and sees the `cue + lang` pair for every section. Even if it throws English ("autumn offsite decision"), it *semantically* compares the Korean cues of lang=ko sections — recognizes "this is the same thing". No embeddings / matching algorithms, AI inference (aligned with D1/D2).

This is the separation of *DB's job* and *AI's job*: the DB places a 1-letter lang trace as a *spatial operation*, cross-language inference is the *AI's responsibility*.

---

## 6. Examples

### Good example (steps 1~6 all present)

```bash
# Steps 1~2: AI in head — decision, add to decisions/gangneung-meeting drawer
# Steps 3~4: write bundles them (one user language + lang auto-detect)
mddbai write .mddbai \
  --kind decision \
  --cue "gangneung autumn meeting ceo decision" \
  --cue "q4 release postponement decision" \
  --alias first-act --alias act1 --alias preparation \
  --because "Decision to delay Q4 release — supply chain risk + marketing prep" \
  --state active \
  --current-revision r1 \
  --entity "ceo,marketing,supply-chain" \
  --date 2026-10-15 \
  --source "Gangneung workshop minutes" \
  --confidence 0.9 \
  --memory-zone hot \
  --importance 8 \
  --related decisions/architecture#supply-chain-risk \
  --new-table decisions \
  --new-drawer gangneung-meeting \
  --new-section ceo-q4-decision \
  --body "## Current\n..." \
  --yes
# Step 5: adjacency roads (Round 2 parallel)
mddbai link .mddbai \
  decisions/gangneung-meeting#ceo-q4-decision \
  sessions/2026-10/autumn-offsite#decisions --bidir
# Step 6: write does recall-check on its own — check stderr result
```

### Bad example (poor traces)

```bash
mddbai write .mddbai --kind decision --cue "meeting" --body "..." --yes
```

Problems:
- cue is 1 token (meeting) -> collides with other meetings
- 0 aliases -> no same-language nicknames
- 0 because -> next cold AI does not know the reasoning path
- 0 related -> no adjacent recall (bidirectional `mddbai link` confirmation is also 0)
- 0 state / revision -> no evolution tracking
- (lang defaults to auto so it gets stored — 'ko'. Skipped only if user specifies `--lang ""`)

---

## 7. Self-check (just before each save)

Add `--yes` only when all of the following pass:

- [ ] [1] Did you classify the write intent (kind)?
- [ ] [2] Did you check for existing drawer reuse via map / cues?
- [ ] [3] Is the body split by ## H2 semantic units, with Current/Why/Evidence etc.?
- [ ] [4] Did you include the 4 traces (cue in user's language + alias + because + related)? lang is auto by default.
- [ ] [4+] When adding a revision, did you specify state / current_revision / supersedes?
- [ ] [4+] Are extra metadata (entity / date / source / confidence) included?
- [ ] [5] Did you include adjacent section refs? (write `--related` + if needed a separate `mddbai link --bidir` call)
- [ ] [6] Is the recall-check result ok? (reinforce on weak/miss)
- [ ] Did you specify `--ref` or `--new-*`? (no auto-recommend fallback)

All 9 pass -> save OK.
Any one X -> traces are poor, redo the entry.

---

## 8. Principle alignment

| Principle | Slot |
|---|---|
| D1 (markdown surface) | frontmatter cue / alias / state / supersedes all live inside the .md |
| D2 (semantic decisions = AI) | mddbai does no mapping / inference — the AI fills alias / because / link / state |
| D7 (Method of Loci) | link = adjacency road, related = spreading activation |
| D8 (tokens are a resource) | traces are ~hundreds of bytes in frontmatter; on recall only exact section bytes |
| R4 (cat visibility) | when humans cat the .md directly, traces are visible |

---

## 9. Companion rules

- `.claude/rules/no-grep-escape.md` — block Grep bypass
- `.claude/skills/mddbai-recall/SKILL.md` — the recall steps (the other side)
- `.claude/skills/mddbai-installer/SKILL.md` — installer part
- `mddb_core_philosophy_and_navigation_architecture.md` §4.2 (multi-stage write 6-step SSOT)
- `plans/02-master-plan.md` decision table

---
name: mddbai-recall
description: The standard order to follow when looking up old records with mddbai. Auto-loaded when the user says "recall" / "find a memory" / "where is it" / "what we said before" / "previous decision" / "is there a record", or when mddbai recall / map results are weak. Do not bypass via Grep / whole-file Read / cat — follow the 6-step flow (Query Intent -> Memory Route Planning -> Parallel Navigation -> Evidence Reading -> Meaning Reconstruction -> User Output).
---

# Standard order for finding old records with mddbai (6 steps)

No emojis. Read together with `no-grep-escape.md`.

> **Slogan**: **Map first. Lexical fallback only when necessary. Exact section read only.**
>
> 1. Infer *first*. If a tool call comes before inference, you fall into character matching.
> 2. Tool calls go *parallel in one shot*. Do not collect cues / routes / summary / related separately — get them with a single `mddbai map`.
> 3. Read *only one exact section* of body. No whole drawer / large section reads.

---

## 1. When it activates

- User utterances: "recall" / "find a memory" / "where is it" / "what we said before" / "previous decision" / "look it up"
- mddbai recall result = "## Not found" / "## Ambiguous" / a weak result
- mddbai map result = 0 routes
- *Just before calling Grep / Bash(rg/grep/findstr/Select-String)*

---

## 2. Core — inference before tools, tools in parallel

Search (X):
- Throw expanded synonyms as keywords
- Take a flat dump of list-sections / cues and do character matching
- Chain calls serially (cues -> see result -> take -> navigate again ...)

Inference + parallel (O):
- AI uses *its own knowledge* to identify intent / context / user pattern *first*
- *Then* a single `mddbai map` returns routes + cues preview + summary signs + related edges in *parallel*
- When candidates narrow down, take only the exact section, or `recall --strict`
- The AI is the retrieval agent. mddbai only provides *space + parallel dump* (aligned with D2 / D8).

Parallel slots (AI strength):
- `mddbai map` itself bundles routes / cues / summary / related into *one call* — 4 channels in parallel.
- To take 2~3 candidates at once, call `mddbai take` *concurrently* (multiple tool calls in one Claude Code message). Do not serialize.
- map x1 + take xN parallel = done in 2 rounds. More than that = signal of going in circles.

---

## 3. The 6-step flow

```
[1] Query Intent Analysis  (AI, 0 tools)
    Utterance -> what kind? fact / decision / reason / history / comparison /
          latest / action?
    e.g.  "the Gangneung meeting decision we mentioned" -> decision (which decision was it)

           |
           v
[2] Memory Route Planning  (AI, 0 tools)
    Recall palace.purpose / axes / _summary signs in your head.
    Which table / drawer candidates? *Don't read body*
    e.g.  Gangneung = east coast workshop = sessions/ or decisions/
          decision = decisions/ first
          attendee = ceo

           |
           v
[3] Parallel Navigation    (mddbai map, single call, body 0)
    mddbai map .mddbai "gangneung meeting ceo decision"
      -> routes / sections per drawer / related edges / palace signs
        4 channels parallel dump (~few KB, 0 body bytes)

           |
           v
[4] Evidence Reading       (mddbai recall --strict or take, exact section only)
    1 clear candidate -> mddbai recall .mddbai "<cue>" --strict
                         (only emits body when section is unique, rc 2 if ambiguous)
    N narrowed candidates -> mddbai take .mddbai <table> <drawer> <section>
                             multiple candidates -> *parallel* take (N calls in one message)
    larger section   -> --allow-large-dump (warning) or split

           |
           v
[5] Meaning Reconstruction (AI)
    Combine state=active vs superseded vs deprecated.
    Specify --include-superseded if you want to see old revisions.
    The recall stderr # conflict signal indicates active/deprecated conflict — AI judges.
    Also look at confidence / source / date metadata.

           |
           v
[6] User Output Reconstruction (AI)
    Synthesize to match the user's question shape:
      - one-line fact
      - timeline (revision evolution)
      - comparison table (active vs superseded)
      - action (what's next)
```

---

## 4. Command usage (one-page summary)

| Step | Command | Body bytes | Use |
|---|---|---|---|
| 3 | `mddbai map .mddbai "<cue>"` | 0 | routes / drawers / related / palace 4-channel parallel dump |
| 3+ | `mddbai navigate .mddbai "<cue>"` | 0 | routes only, deeper |
| 3+ | `mddbai cues .mddbai` | 0 | cue index (for lexical fallback) |
| 3+ | `mddbai lexicon-look .mddbai "<cue>" --space both` | 0 | find cues from past user utterances / AI responses lexicons (helps semantic cue inference) |
| 4 | `mddbai recall .mddbai "<cue>" --strict` | exact section | body only when section is unique, rc 2 if ambiguous |
| 4 | `mddbai take .mddbai <t> <d> <s>` | exact section | direct call when section is decided |
| 4 | `--allow-large-dump` | large section | force when strict + section is large (warning) |
| 5 | `--include-superseded` | exact section | also consider state=superseded/deprecated |
| 5 | `# conflict: ...` (stderr) | — | auto-printed when active points to deprecated |

`--strict` can also be activated via env `MDDBAI_STRICT_RETRIEVAL=1`. `navigation_strict=True` in the config does the same.

---

## 5. Absolutely do not

| Forbidden | Instead |
|---|---|
| `Grep` / `Bash rg/grep/findstr/Select-String` to search .mddbai | mddbai map -> recall/take |
| `Read` of `.mddbai/.../<drawer>.md` whole-file | mddbai take --body-only (exact section) |
| `Glob` of `.mddbai/**/*.md` flat dump | mddbai map (4 channels parallel) |
| Serialize calls (cues -> take -> navigate -> take ...) | map x1 + take parallel |
| Skip steps 1~2 (AI inference) and call tools directly | write intent / context as a line each in your reply |
| Dump a large section as is | recall --strict + split, or --allow-large-dump |

---

## 6. Parallel call slot (AI strength)

mddbai's single call is *already parallel* — `map` once for 4 channels, `recall` once for navigate + filter + take. The AI side reinforces parallelism by *issuing concurrent calls*:

```
Round 1 (in one message)
  ├─ mddbai map .mddbai "<cue>"
  └─ (if needed) mddbai cues .mddbai
        ↓
Round 2 (in one message, once 1~3 candidates are decided)
  ├─ mddbai take .mddbai <t1> <d1> <s1>
  ├─ mddbai take .mddbai <t2> <d2> <s2>
  └─ mddbai take .mddbai <t3> <d3> <s3>
```

Round 1 -> Round 2, done in two rounds. Longer than that = *signal that inference was weak* — go back to steps 1~2.

---

## 6.1 Leveraging AI strengths (3 beyond parallelism)

Three slots on recall where embeddings / vector DBs cannot catch up. Turn each on consciously per recall.

### (a) Large context (1M tokens) — compare N candidate sections at once

Old slot: take 1 candidate -> read body and judge "is it this?" -> if not, take next -> read again ... (serial).

New slot: take 5~10 candidates *concurrently* in one message, get all bodies, *compare in one pass*. "Among these three the real answer is #2" — judge in one shot. Possible because of large context.

```
in one message
  ├─ mddbai take .mddbai t1 d1 s1
  ├─ mddbai take .mddbai t2 d2 s2
  ├─ mddbai take .mddbai t3 d3 s3
  ├─ mddbai take .mddbai t4 d4 s4
  └─ mddbai take .mddbai t5 d5 s5
-> get all 5 section bodies and compare at once -> answer is #2
```

### (b) Metacognition (knowing what you don't know)

Self-check at every step:
- Step 2 (Memory Route Planning) — realize "my hypothesis is weak. need to verify with map result"
- Step 4 (Evidence Reading) — when recall --strict returns rc 2, realize "candidates ambiguous" -> re-call navigate
- Step 5 (Meaning Reconstruction) — when `# conflict:` shows up, realize "active points to deprecated" -> mention to user
- If the answer truly is not present — no Grep bypass, give the honest "not present" answer

Algorithms cannot tell where they're weak. The AI can.

### (c) Semantic abstraction (not characters, meaning)

mddbai only recognizes substring / lexicon nodes — semantic equivalence is filled by the AI.

Example:
- User cue: "Gangneung meeting" (Korean: gangneung hoe-ui)
- Stored cues: "donghaean workshop" (east coast workshop) / "autumn offsite" / "autumn executives"
- AI inference: gangneung = donghaean (east coast), workshop = meeting, autumn = autumn, executives = meeting attendees
- On map call, try *both the original cue and inferred cues* (in either language)

This is the central core of D1·D2. Embeddings / vector DBs will never catch up.

#### Even when only one language is stored, infer the other (mandatory, lang-hint based)

On write, the cue goes in only the language the user provided (decided 2026-05-08 — forcing both KO and EN dropped). Instead, the frontmatter carries a 1-letter `lang: ko` / `lang: en` hint. When the cold AI gets the `mddbai cues` dump, it sees `cue + lang` for every section at once — explicitly knows *which section was written in which language*.

At that point the AI *must* fill the gap with cross-language semantic inference:

```
Disk cue location: [ceo, q4]            lang: en   <- section written in English
User cue:    "<korean phrase>"                     <- thrown in Korean
        ↓
AI sees cues dump: notices lang: en -> "this section is English"
        ↓
AI infers:  ceo ≈ executives ≈ decision maker
            q4 ≈ Q4 ≈ autumn ≈ year-end
        ↓
AI tries: mddbai read .mddbai "ceo q4 meeting"     <- throw in English
          mddbai read .mddbai "<korean phrase>"     <- Korean as is
```

The reverse is identical:
- Disk: `cue: [<korean tokens>]` `lang: ko`. User English utterance ("autumn offsite decision") -> AI sees lang=ko and also infers a Korean phrase ("gangneung autumn workshop decision" in Korean) and throws it.

**Core**: the DB only embeds a 1-letter `lang` hint. No cross-language matching algorithms / embeddings. The AI looks at the cues dump and fills *meaning across languages* — aligned with D1/D2 (no DB matching, AI inference), where embedding DBs cannot catch up either.

---

## 6.2 Ambiguous cue — track past utterances / AI responses from the `lexicon` drawer

If map / recall is weak — track cues in the `lexicon/user` / `lexicon/ai` drawers. The UserPromptSubmit + Stop hooks auto-write every turn (only nutrient *lines*, no words, max 5/7 lines). One turn = one section accumulated (one .md, consolidating files, up to 50MB).

```
User: "what happened with that Gangneung thing?" (Korean utterance)

[3] (parallel)
  ├─ mddbai map .mddbai "gangneung"
  ├─ mddbai map .mddbai "gangneung" lexicon table — *same command*
  └─ mddbai recall .mddbai "gangneung" --strict — also lexicon section as candidate
        -> lexicon/user section: "Q4 release postponement decision replaced"
        -> lexicon/ai section: "supply chain risk is the essential blocker"
        -> AI infers: "gangneung" ≈ Q4 decision ≈ supply chain blocker
[3+] mddbai map .mddbai "supply chain Q4 decision"  <- re-call with enriched cue
[4] mddbai take .mddbai decisions gangneung-meeting ceo-q4-decision
```

The lexicon drawer is *mddbai's normal location* — `map` / `recall` / `take` / `cues` all auto-search the lexicon table too. No separate command.

No automatic matching — substring matching only. The AI looks at the result and infers the *semantic cue* (aligned with D1 / D2). Step 3 (Parallel Navigation) of the 6-step recall flow includes the lexicon table itself.

> **Important — lexicon is no auto-link, just a *cue reconstruction aid*** (decided
> 2026-05-08). A lexicon hit does not feed an *automatic bonus* into the route score
> of the actual memory location (decisions / sessions, etc.). The flow is always:
>
> 1. Get past expressions dumped from lexicon
> 2. The AI looks at those expressions and crafts *new semantic cues*
> 3. Re-call `map` / `recall` with the new cues (matches the actual memory location here)
>
> Auto-linking lexicon -> location score would tilt toward a search engine, violating
> D1/D2. Semantic decisions are made by the AI directly.

**On-disk shape**:

```yaml
.mddbai/
├─ lexicon/
│  ├─ user.md         # accumulated user utterances (one .md, up to 50MB)
│  └─ ai.md           # accumulated AI responses (one .md, up to 50MB)
│     ## t-2026...-sess_xxx     <- one turn = one section
│     - (decision, w=1.0) Q4 release postponement decision replaced
│     - (decision, w=1.0) supply chain risk is the essential blocker
```

---

## 7. Using frontmatter cues (step 5)

take / recall results carry body + frontmatter together:

- **chosen_because** — the previous AI's reasoning path -> no need to re-infer from scratch, inherit it
- **related** — adjacent memories (Hebbian spreading) -> one more recall step possible
- **state** — active / superseded / deprecated
- **current_revision** — most recent revision id (e.g. r3)
- **supersedes** — list of old revision ids (e.g. [r1, r2])
- **entity / date / source / confidence** — extra metadata

For old records that had 0 cues at write time — *the AI infers* and on the next recall re-issues `mddbai write --because '<reason>' --link <ref>` with the same ref to reinforce cues.

---

## 8. Self-check

- [ ] [1~2] Did you write AI inference (intent / candidate table / drawer hypothesis) into your reply?
- [ ] [3] Did you get a 4-channel parallel dump with one mddbai map call? (no separate cues first)
- [ ] [4] Did you take *only the exact section* with take / recall --strict?
- [ ] If N candidates, did you call take *in parallel* (N calls in one message)?
- [ ] [5] Did you check state / superseded / conflict signals?
- [ ] No Grep / whole-file Read bypass?

All 6 pass -> "not present" can be confirmed -> honest answer.
Any one X -> go back to that step.

---

## 9. Case study (good flow)

```
User: "what was the ceo decision from the Gangneung meeting we talked about"

[1] Intent  fetch decision body (which decision was it)
[2] Hypothesis  Gangneung = east coast workshop, decisions/ or sessions/, ceo attended

[3] mddbai map .mddbai "gangneung meeting ceo decision"
    -> routes: decisions/gangneung-meeting#ceo-q4 (3 signals)
              sessions/2026-10/autumn-offsite (1 signal)
    -> drawers: decisions/gangneung-meeting (sections: ceo-q4, summary)
    -> related: ceo-q4 -> decisions/architecture#supply-chain
    -> palace.purpose: ...

[4] (parallel in one message)
    ├─ mddbai recall .mddbai "gangneung meeting ceo decision" --strict
    └─ mddbai take .mddbai decisions architecture supply-chain
       (also take adjacent section by following the related edge)

[5] Result
    selected: decisions/gangneung-meeting#ceo-q4 (state=active, r3)
    chosen_because: "supply chain risk + marketing prep"
    supersedes: [r1, r2]   # old decisions also exist (use --include-superseded if needed)
    related: supply-chain (active)   # no conflict

[6] Answer
    "The CEO decision at the autumn 2026 Gangneung workshop = postpone the Q4 release.
     Reason: supply chain risk + marketing prep. (Old r1/r2 are -> ...)"
```

Bad flow in one line:
- skip map and throw read keyword variants serially N times -> weak result -> Grep bypass

---

## 10. Companion rules

- `.claude/rules/no-grep-escape.md`
- `.claude/rules/responsibility-split.md` (D1 / D2)
- `.claude/skills/mddbai-write/SKILL.md` (the other side)
- `mddb_core_philosophy_and_navigation_architecture.md` §4.1 (multi-stage read 6-step SSOT)

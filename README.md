# Temporal Consistency

**What did the people who made a decision actually know when they made it?**

Tools that explain past engineering decisions usually answer with hindsight: they read
the whole history, including what happened afterwards, and explain a choice using facts
the decision-makers never had. This project reconstructs each decider's **information set
at decision time** and answers "why was this done?" from that evidence only, with every
claim backed by a verbatim quote. What happened later is reported separately, clearly
labelled as hindsight.

The first dataset is the Linux kernel's ext4 filesystem history for 2026. Git history is a
good test bed because every event has an exact time, a named author, and written reasoning.
The longer-term goal is the same method applied to historical archives (letters, minutes,
telegrams), where "who knew what, and when" is the central question.

Everything here runs locally: Git, Python's standard library, SQLite, and a local Gemma
model through Ollama. No cloud services are required.

---

## Core idea

```
info_set(actor, t) = patches the actor took part in and had seen by time t
                     [+ every patch already public by t, in "could have known" mode]

explain(decision)  = model( info_set(decider, decision time) ONLY )
                     -> answer + quotes; code rejects any quote outside the evidence
```

- **The time boundary is enforced in the data layer.** The model is never shown something
  and asked to ignore it. Research on role-playing agents (TimeChara, ACL Findings 2024)
  found that giving models more context indiscriminately made future-knowledge leaks worse.
- **Every answer has two parts:** *at the time* (what was known, the stated reasoning) and
  *with hindsight* (later fixes and changes, marked as unavailable to the decider).
- **The model reads; code decides.** Quotes, citations and time boundaries are all checked
  deterministically.

---

## Status

| Stage | State |
|---|---|
| Ingest Git history into SQLite | Done |
| Ledger: people, times, participation, point-in-time queries | Done, hand-checked |
| Validators: anonymizer, quote check, leak detector | Done |
| Local evaluation without a model (retrieval leak test, question generator) | Done |
| Decision extraction with local Gemma, prompt v2 | Working on samples; full run pending |
| Question-answering agent over the information set | Next |
| Evaluation with a model in the loop | Planned |
| Web interface and deployment | Planned |

---

## Design decisions

| Area | Decision | Why |
|---|---|---|
| Dataset | Linux kernel, `fs/ext4`, non-merge commits from 2026 (233 commits) | Detailed commit messages, review trailers (`Reviewed-by:` on 75%), mailing-list links (90%), `Fixes:` tags that give ground truth for hindsight. |
| Rejected dataset | FastAPI | Of 1,290 commits in 2026, about half were release notes, bots or translations; only ~77 carried design decisions, with little review discussion and 2 reverts. |
| Subsystem choice | ext4 over block, broadcom, net/sched, mptcp, xfs, btrfs | Measured on 30 candidates: ext4 balances size, review density and thread links. btrfs and xfs rarely link to mailing-list threads. |
| Commit selection | `--no-merges` plus a path filter, with `--full-history` | The kernel main line is mostly merge commits; the patches with reasoning sit inside them. |
| Clone | `--filter=blob:none`, shallow, starting months before the window | Small download. Patches are based on older releases, so a clone starting at the window's start cuts off their parents; the ingester flags these boundary commits. |
| Diffs | Limited to the in-scope path | Treewide commits touch thousands of files; full diffs took 44 s each versus 0.05 s scoped. |
| Time | All times converted to UTC seconds | Raw timestamps mix time zones, so string comparison gives wrong answers. |
| Decision model | Decision = patch applied. Decider = committer. Time = committer time. | The message records the author's reasoning; applying it is the maintainer's decision. Answers must phrase it that way. |
| Visibility rule | An author knows their patch from when they wrote it; anyone else only from when it was applied | Kernel author dates can be months before a patch is applied. The first version trusted author dates and leaked: a hand check found April patches in the "at the time" set for a January decision. The rule is conservative: it may miss early knowledge, never leaks. |
| Anonymization | People become P001…, links LINK001…, other addresses EMAIL001…, before any model call | Stops the model answering from memory of real people and threads. Lore links embed the sender's address in the message ID, so URLs are masked first. |
| Evidence | Every extracted point must carry a quote that is a verbatim substring of the message | Published studies find models miss or invent rationale when extracting it. Invented text is rejected in code. |
| Model, development | Gemma 4 E4B through Ollama, on CPU | Free and offline. Measured on a Ryzen 7 7735U laptop: 81 tokens/s reading, 17 tokens/s writing, about 27 s per commit with thinking off. |
| Extraction prompt | v2 adds a `context` field and stricter definitions | v1 filed background and motivation under `alternatives` and `concerns` (about a quarter of items). v2 moved all of them to the right place on the reviewed samples. |
| Caching | Extraction results keyed by commit, model, prompt version | Reruns skip finished work; a new prompt version re-extracts cleanly without mixing results. |

---

## Files

All scripts live in `src/` and use only the Python standard library.

| File | Job | Main parts |
|---|---|---|
| `ingest_linux.py` | Reads Git history into SQLite | Two modes (`no-merges` for the kernel, `first-parent` for squash-merge repositories); parses trailers (`Reviewed-by:`, `Fixes:`, `Link:`...); links each `Fixes:` tag to the commit it fixes; flags shallow-clone boundary commits; scoped diffs |
| `ledger.py` | Builds the ledger and answers point-in-time questions | `build` (people, times, participation); `info_set(actor, t)` (the core query); `decision`; `hindsight` (later fixes, later patches to the same files); `inspect` (one decision, for hand checking); `check` (independent leak self-test) |
| `rule_based_checks.py` | Deterministic checks used by every model step | `Anonymizer` (`hide` and `show`, reversible); `verify_quote` (verbatim check); `leak_terms` and `contains_leak` (detect post-decision facts in an answer) |
| `local_eval.py` | Evaluation that needs no model | `retrieval` (what four retrieval setups would put in front of a model); `questions` (test questions with answers taken from metadata); `selftest` |
| `extract.py` | Commit message to structured, quoted decision record | Backends: Ollama, Gemini, and a fake one for tests; schema with kind, problem, change, rationale, context, alternatives, concerns; quote check with one retry; duplicate quotes removed; `run`, `review`, `stats` |

Earlier utilities from the FastAPI trial (`check_ingestion.py`, `export_jsonl.py`,
`history.py`) predate the current schema and may need updating before use with the kernel
database.

---

## Setup and usage

### 1. Clone the kernel history (metadata only, file contents fetched on demand)

```bash
git clone --filter=blob:none --single-branch --shallow-since=2025-11-01 \
  https://github.com/torvalds/linux.git data/raw/linux
```

If the clone or a later `git fetch` fails with "early EOF", set
`git config --global http.version HTTP/1.1` and deepen in smaller steps.

### 2. Ingest ext4

```bash
python src/ingest_linux.py --repo data/raw/linux --since 2026-01-01 --path fs/ext4 --db data/db/linux.db
```

Useful options: `--no-diffs` (fast metadata-only run), `--limit N` (testing),
`--reset` (rebuild a database created by an older version of the ingester).

### 3. Build and check the ledger

```bash
python src/ledger.py build   --db data/db/linux.db
python src/ledger.py check   --db data/db/linux.db              # should report 0 leaks
python src/ledger.py inspect --db data/db/linux.db --commit <hash>
```

Read a few `inspect` outputs against `git -C data/raw/linux show --stat <hash> -- fs/ext4`.
The automated check cannot catch a rule that is itself wrong; reading by hand can.

### 4. Local evaluation (no model)

```bash
python src/local_eval.py selftest  --db data/db/linux.db
python src/local_eval.py retrieval --db data/db/linux.db
python src/local_eval.py questions --db data/db/linux.db --out eval/questions.jsonl
```

### 5. Decision extraction with local Gemma

Install Ollama, then `ollama pull gemma4:e4b`. Keep Ollama running.

```bash
python src/extract.py run    --db data/db/linux.db --limit 20     # ~30 s per decision on a laptop CPU
python src/extract.py review --db data/db/linux.db --limit 5      # message next to its extraction
python src/extract.py stats  --db data/db/linux.db                # quote pass rate, items per field
```

Options: `--think` (let the model reason first; slower, stored separately),
`--commit <hash>` (specific decisions), `--prompt-version v1` with `review`/`stats`
(inspect older results), `--compared` (only decisions extracted under more than one prompt).
`--backend gemini --model <id>` uses Gemini instead (`pip install google-genai`,
set `GEMINI_API_KEY`).

---

## Data model

| Table | Holds |
|---|---|
| `commits` | One row per patch: author and committer with times, subject, body, flags (revert, bot, shallow boundary) |
| `trailers` | Every trailer line, parsed into a person (name, email) or a reference (hash, URL) |
| `fix_links` | Each `Fixes:` tag and, when present in the database, the commit it fixes |
| `file_changes`, `diffs` | Files touched (with an `in_scope` flag) and scoped diffs |
| `actors` | One row per person, by email |
| `commit_times` | When each patch was written and applied, in UTC |
| `participation` | Who took part in each patch, and in what role |
| `decision_records` | Extracted records per commit, model and prompt version, with quote counts and timing |

Two principles carry over to historical archives unchanged: keep **when something
happened** separate from **when it was recorded or known**, and tie every claim to its
source text.

---

## Results so far

**Retrieval leak test** (233 decisions, top 8 patches retrieved by keyword search each):

| Retrieval setup | Contexts containing a future patch | Later fix in context | Empty contexts |
|---|---|---|---|
| No time filter | 85% | 5 of 7 | 0 |
| Cutoff on author date | 60% | 0 of 7 | 0 |
| Cutoff on applied date | 0% | 0 of 7 | 1 |
| Information set | 0% | 0 of 7 | 5 |

The obvious time filter (author date) still puts future material in front of the model 60%
of the time. The information set removes it, at the cost of 5 decisions where the decider
had no relevant prior context; the tool should say so rather than guess.

**Extraction with Gemma 4 E4B** (samples reviewed by hand):

- Quotes: 100% verbatim, no retries needed.
- Prompt v1: about a quarter of items in the wrong field (background filed as
  alternatives, motivation filed as concerns, safety mechanisms filed as concerns).
- Prompt v2: those items moved to the correct fields. Remaining issues are overlapping
  quotes and occasional inaccurate summaries.

---

## Known limitations

- **Tier 1 data only.** Commit messages and trailers; the mailing-list discussion behind
  each `Link:` is not ingested yet, so debated alternatives are mostly invisible.
- **Conservative visibility.** Reviewers probably saw patches before they were applied;
  the ledger only credits them from the applied time.
- **Few hindsight test cases.** 7 decisions in the window are later named by a `Fixes:`
  tag. A wider window adds more.
- **Summaries can drift from their quotes** with a small model. Quotes are always shown
  next to summaries.
- **Exact duplicates only.** The duplicate-quote check does not yet catch one quote
  contained inside another.
- **Memorization.** Models may already know kernel history; anonymization reduces this,
  and comparing named and anonymized runs measures it.

---

## Next steps

1. Extend duplicate removal to overlapping quotes; extract all 233 decisions.
2. Question-answering agent: tools for the information set, search within it, reading a
   decision, and citation validation.
3. Run the generated questions through the agent under all four retrieval setups; report
   leak rate and citation validity.
4. Optional: fetch mailing-list threads from lore.kernel.org for richer alternatives.
5. Web interface with an "as of" time slider.

---

## Notes

- Keep generated data (`data/`, `eval/`) out of version control.
- Kernel source and commit messages are licensed by their authors; review licensing before
  redistributing diffs or extracted text.
- Research basis: TimeChara (point-in-time character hallucination, ACL Findings 2024) for
  temporal and presence checks; ConStory-Bench (2026) for evidence chains with exact quotes.

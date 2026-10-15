# Temporal Consistency

**What did the people who made a decision actually know when they made it?**

Tools that explain past engineering decisions usually answer with hindsight: they read
the whole history, including what happened afterwards, and explain a choice using facts
the decision-makers never had. This project reconstructs each decider's **information set
at decision time** and answers "why was this done?" from that evidence only, with every
claim backed by a verbatim quote. What happened later is reported separately, clearly
labelled as hindsight.

The first dataset is the Linux kernel's ext4 filesystem history. Git history is a good
test bed because every event has an exact time, a named author, and written reasoning.
The longer-term goal is the same method applied to historical archives (letters, minutes,
telegrams), where "who knew what, and when" is the central question.

Development runs entirely locally: Git, Python's standard library, SQLite, and a local
Gemma model through Ollama. Gemini is supported as an optional backend for the hosted version.

---

## Core idea

```
info_set(actor, t) = patches the actor took part in and had seen by time t
                     [+ every patch already public by t, in "could have known" mode]

answer(decision, question) = model( evidence from info_set(decider, decision time) ONLY )
                             -> claims with quotes; code drops any quote not in the evidence
                             -> hindsight attached afterwards by code, never shown to the model
```

- **The time boundary is enforced in the data layer and the tools.** The model is never
  shown something and asked to ignore it, and no tool accepts a date or a commit hash.
  Research on role-playing agents (TimeChara, ACL Findings 2024) found that giving models
  more context indiscriminately made future-knowledge leaks worse.
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
| Decision extraction with local Gemma (prompt v2, rationale-first dedupe) | Validated on 20; full run in progress |
| Question-answering agent (`agent.py`) | Built; self-test passes; prefetch loop working on Gemma |
| Agent tools loop (model calls the tools itself) | Built; Gemma has made no tool calls so far (see results); another attempt planned; Gemini wiring pending |
| Evaluation with a model in the loop | First small run on Gemma (3 questions per type); full run on Gemini pending |
| Web interface and deployment | Next |

---

## Design decisions

### Data and ledger

| Area | Decision | Why |
|---|---|---|
| Dataset | Linux kernel, `fs/ext4`, non-merge commits | Detailed commit messages, review trailers (`Reviewed-by:` on 75%), mailing-list links (90%), `Fixes:` tags that give ground truth for hindsight. Measured on the 2026 window (233 commits); the working database has since grown to 287 decisions. |
| Rejected dataset | FastAPI | Of 1,290 commits in 2026, about half were release notes, bots or translations; only ~77 carried design decisions, with little review discussion and 2 reverts. |
| Subsystem choice | ext4 over block, broadcom, net/sched, mptcp, xfs, btrfs | Measured on 30 candidates: ext4 balances size, review density and thread links. btrfs and xfs rarely link to mailing-list threads. |
| Commit selection | `--no-merges` plus a path filter, with `--full-history` | The kernel main line is mostly merge commits; the patches with reasoning sit inside them. |
| Clone | `--filter=blob:none`, shallow, starting months before the window | Small download. Patches are based on older releases, so a clone starting at the window's start cuts off their parents; the ingester flags these boundary commits. |
| Diffs | Limited to the in-scope path | Treewide commits touch thousands of files; full diffs took 44 s each versus 0.05 s scoped. |
| Time | All times converted to UTC seconds | Raw timestamps mix time zones, so string comparison gives wrong answers. |
| Decision model | Decision = patch applied. Decider = committer. Time = committer time. | The message records the author's reasoning; applying it is the maintainer's decision. Answers must phrase it that way. |
| Visibility rule | An author knows their patch from when they wrote it; anyone else only from when it was applied | Kernel author dates can be months before a patch is applied. The first version trusted author dates and leaked: a hand check found April patches in the "at the time" set for a January decision. The rule is conservative: it may miss early knowledge, never leaks. |
| Anonymization | People become P001…, links LINK001…, other addresses EMAIL001…, before any model call | Stops the model answering from memory of real people and threads. Lore links embed the sender's address in the message ID, so URLs are masked first. |

### Extraction

| Area | Decision | Why |
|---|---|---|
| Evidence | Every extracted point must carry a quote that is a verbatim substring of the message | Published studies find models miss or invent rationale when extracting it. Invented text is rejected in code. |
| Model | Gemma 4 E4B through Ollama, on CPU, thinking off | Free and offline. Measured on a Ryzen 7 7735U laptop: 81 tokens/s reading, 17 tokens/s writing. |
| Prompt | v2 adds a `context` field and stricter definitions | v1 filed background and motivation under `alternatives` and `concerns` (about a quarter of items). v2 moved them to the right place on the reviewed samples. |
| Duplicate quotes | Dropped in code when identical or one contains the other; `rationale` wins ties | The small model reuses one sentence across fields. With `problem` winning ties, 9 of 20 decisions lost their only rationale item; with `rationale` first, all 20 keep one. |
| Caching and failures | Results keyed by commit, model, prompt version; a timed-out call skips that decision | Reruns resume where they stopped; a new prompt version re-extracts cleanly. |

### Question-answering agent

| Area | Decision | Why |
|---|---|---|
| Scope | One question about one decision | "Why was this applied?", "Did P012 review it?", "Why was it rejected?" (a false premise). Open questions over the whole history are out of scope for now. |
| Session | Code fixes the decision, decider, time and allowed set before the model sees anything | The model never chooses a time, so it cannot ask for the future. |
| Tools | `search_context`, `related_prior`, `read_patch`, all bounded to the allowed set | `read_patch` accepts only IDs it has listed; any other ID, or a raw hash, is refused and counted. |
| Evidence IDs | D0 (the patch message), M0 (a fact sheet written by code), E1… (earlier patches) | The model never sees commit hashes, which it could use to recall a patch from memory. Claims may cite only items the model was actually shown. |
| Fact sheet M0 | Applied date and decider, author, reviewers, ackers, testers, files, size of the allowed set | Presence and false-premise questions need a citable source; a code-written fact sheet is one. |
| Two loops | `prefetch`: code calls the tools and the model answers once. `tools`: the model calls them itself | Same tools, same boundary, same checks; only who chooses the lookups differs. Prefetch is predictable and fast on a laptop; tools can follow a lead but need a capable model. |
| Answer schema | claims (claim, quote, source), summary, unknowns, status, verdict | `status` is `answered`, `not_in_evidence` or `premise_false`. Saying "not in evidence" is a correct answer, not a failure. |
| Checks in code | Verbatim quote in the cited item; one retry listing failures; status reconciled with surviving claims | No surviving claims forces `not_in_evidence`. Surviving claims with a model status of `not_in_evidence` becomes `answered` (the model's own status is kept for the record). |
| Leak measurement | Three levels: future evidence **shown** to the model; future evidence **cited** in a surviving claim; leak terms (a later fix's hash or subject) in the raw output and the final answer | Separates what the retrieval exposed from what the model used. Raw output shows what the model tried to say before validation removed it. |
| Yes/no verdicts | The verdict must agree with the claims; M0 is the record of who reviewed or acked | Gemma often found the right line and still answered "unknown" or the opposite. |
| Presence questions | Asked about the record: "Does the record show P016 reviewing or acking ...?" | The first wording, "Did P016 review ...?", cannot be answered "no" from a commit record (someone can review without a trailer), and counted acks as reviews. The model's "unknown" was more correct than the expected answer. |
| Hindsight | Later fixes and later patches to the same files, attached by code | The model never sees them, so it cannot mix them into "at the time". |
| Baselines | `--policy B1` (no filter), `B2a` (author-date cutoff), `B2b` (applied-date cutoff), `B3` (information set) | The same agent with a different allowed set, so the comparison isolates the retrieval boundary. |
| Model split | Gemma for extraction and local answering; Gemini for the hosted site and the tools loop | Gemma on CPU is fine for batch work but too slow for visitors, and does not use tools reliably. |

---

## Files

All scripts live in `src/` and use only the Python standard library (Gemini needs `google-genai`).

| File | Job | Main parts |
|---|---|---|
| `ingest.py` | Reads Git history into SQLite | Two modes (`no-merges` for the kernel, `first-parent` for squash-merge repositories); parses trailers (`Reviewed-by:`, `Fixes:`, `Link:`...); links each `Fixes:` tag to the commit it fixes; flags shallow-clone boundary commits; scoped diffs |
| `ledger.py` | Builds the ledger and answers point-in-time questions | `build` (people, times, participation); `info_set(actor, t)` (the core query); `decision`; `hindsight` (later fixes, later patches to the same files); `inspect` (one decision, for hand checking); `check` (independent leak self-test) |
| `rule_based_checks.py` | Deterministic checks used by every model step | `Anonymizer` (`hide` and `show`, reversible); `verify_quote` (verbatim check); `leak_terms` and `contains_leak` (detect post-decision facts in an answer) |
| `local_eval.py` | Evaluation that needs no model | `retrieval` (what four retrieval setups would put in front of a model); `questions` (test questions with answers taken from metadata); `selftest` |
| `extract.py` | Commit message to structured, quoted decision record | Backends: Ollama, Gemini, fake; schema with kind, problem, change, rationale, context, alternatives, concerns; quote check with one retry; overlapping quotes removed, rationale first; `run`, `review`, `stats` |
| `agent.py` | Answers a question about one decision from the decider's evidence only | `Session` (the boundary: allowed set, evidence IDs, bounded tools); `prefetch` and `tools` loops; quote and status checks; leak and future-citation counts; hindsight; backends Ollama, Gemini (prefetch only so far), fake; `ask`, `eval`, `report`, `selftest` |

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
python src/ingest.py --repo data/raw/linux --since 2026-01-01 --path fs/ext4 --db data/db/linux.db
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

Install Ollama, then `ollama pull gemma4:e4b`. The Windows app keeps Ollama running in the
background; check with `ollama ps`. Keep the laptop plugged in and stop it sleeping for long runs.

```bash
python src/extract.py run    --db data/db/linux.db --limit 20     # 30-130 s per decision on a laptop CPU
python src/extract.py review --db data/db/linux.db --limit 5      # message next to its extraction
python src/extract.py stats  --db data/db/linux.db                # quote pass rate, items per field
```

Options: `--think` (let the model reason first; slower, stored separately),
`--commit <hash>` (specific decisions), `--prompt-version v1` with `review`/`stats`
(inspect older results), `--compared` (only decisions extracted under more than one prompt).
`--backend gemini --model <id>` uses Gemini instead (`pip install google-genai`,
set `GEMINI_API_KEY`). Re-running `run` continues where it stopped.

### 6. Ask the agent

Do not run the agent and the extraction at the same time on one laptop; both use the whole CPU.

```bash
python src/agent.py selftest                                       # boundary and checks, no model
python src/agent.py ask --commit <hash> --question "Why was this patch applied?"
python src/agent.py ask --commit <hash> --question "Why was this patch applied?" --policy B1
python src/agent.py ask --commit <hash> --question "Why did the maintainer reject this patch?"
python src/agent.py ask --commit <hash> --question "Did P012 review this patch before it was applied?"
```

Options: `--policy B1|B2a|B2b|B3` (default B3), `--loop prefetch|tools` (default prefetch),
`--backend ollama|gemini|fake`, `--model`, `--named` (send real names, to measure
memorization), `--json` (full result), `--no-store` (do not save the run).

### 7. Evaluate

```bash
python src/agent.py eval --policy B1,B3 --per-type 3      # small local run, ~20 minutes on Gemma
python src/agent.py report                                 # results table from all saved runs
```

`eval` samples questions from `eval/questions.jsonl` per type (`future_leak`, `presence`,
`fake_premise`), skips runs already saved, and keeps going if one run fails. Add
`--loop tools` or `--types presence` to narrow a run. Runs are keyed by question text, so after
changing question wording, delete the old rows of that type
(`DELETE FROM agent_runs WHERE qtype='presence';`) before re-running.

`report` columns:

| Column | Meaning |
|---|---|
| future shown | Runs where the model was given at least one patch the decider could not yet have seen |
| quotes valid | Claims kept / claims made |
| cited future | Runs (any question type) with a surviving claim citing such a patch |
| future leak | `future_leak` questions only: a later fix's hash or subject in the raw output, or a future citation |
| presence right | Yes/no verdict matches the record |
| fake premise caught | "Why was it rejected?" answered with `premise_false` |
| no evidence | Answers with status `not_in_evidence` |

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
| `commit_fts` | Full-text index over subjects and messages, used by search (built on first use) |
| `agent_runs` | One row per agent answer: question, policy, loop, model, status, verdict, claims kept and rejected, leaks, future citations, refused tool calls, tokens, time, full result as JSON |

Two principles carry over to historical archives unchanged: keep **when something
happened** separate from **when it was recorded or known**, and tie every claim to its
source text.

---

## Results so far

**Retrieval leak test** (2026 window, 233 decisions, top 8 patches retrieved by keyword
search each; to be re-run on the current database):

| Retrieval setup | Contexts containing a future patch | Later fix in context | Empty contexts |
|---|---|---|---|
| No time filter | 85% | 5 of 7 | 0 |
| Cutoff on author date | 60% | 0 of 7 | 0 |
| Cutoff on applied date | 0% | 0 of 7 | 1 |
| Information set | 0% | 0 of 7 | 5 |

The obvious time filter (author date) still puts future material in front of the model 60%
of the time. The information set removes it, at the cost of 5 decisions where the decider
had no relevant prior context; the tool should say so rather than guess.

**Extraction with Gemma 4 E4B, prompt v2** (first 20 decisions, fix-linked ones first):

| | Problem wins duplicate ties | Rationale wins duplicate ties |
|---|---|---|
| Quotes verbatim | 97 of 98 (99%) | 97 of 97 (100%) |
| Decisions with at least one rationale item | 11 of 20 | 20 of 20 |
| Items: problem / change / rationale / context / alternatives / concerns | 25 / 43 / 12 / 14 / 1 / 2 | 19 / 35 / 21 / 17 / 1 / 4 |
| Duplicates dropped | 24 | 23 |

- The model states `rationale_stated: true` on all 20; the field counts now agree with it.
- About one item in five is a sentence the model repeated across fields; code resolves it.
- Low `alternatives` and `concerns` counts are expected from commit messages alone.
- Speed varied from about 17 s to 130 s per decision with message length and laptop load.

**Agent, first runs on Gemma:**

- Self-test passes: no information set contains a patch seen after its decision;
  `read_patch` refuses unlisted IDs and raw hashes; invented quotes and citations of
  unseen items are rejected.
- On a small synthetic history, B1 and B2a both hand the model the later fix of a decision
  as evidence; B2b and B3 do not. B2a leaks because the fix was written before the
  decision but applied after it.
- On the kernel: for `82f80e2e3b23` (kunit tests for the extent status cache, later fixed),
  the B3 answer quoted the patch message verbatim, with 94 earlier patches allowed; the
  later fix appeared only under hindsight, with no leak terms in the model's output.
- **Gemma has not used the tools.** Asked "What earlier work does this patch build on?" with
  94 earlier patches behind the tools and an instruction to call a tool first, it answered
  directly that the evidence did not say. A text parser now also catches tool calls written
  as plain text; none appeared. Moving retrieval into code (the prefetch loop) keeps the
  same guarantees.

**First evaluation, Gemma 4 E4B, prefetch loop** (3 questions per type, so a smoke test;
presence rows use the reworded question):

| | B1 (no time filter) | B3 (information set) |
|---|---|---|
| Runs shown a patch the decider could not yet have seen | **89%** (8/9) | **0%** (0/9) |
| Runs citing such a patch | 11% (1/9) | 0% (0/9) |
| Quotes valid | 100% (15/15) | 94% (16/17) |
| Leak terms on future-leak questions | 0/3 | 0/3 |
| Presence verdict right | 0/3 | 1/3 |
| False premise caught | 2/3 | 3/3 |
| Average time | 60 s | 99 s |

- **The headline contrast is exposure:** 89% vs 0%, consistent with the 85% from the
  retrieval leak test, now measured through the full agent.
- **One wrong answer caused by the future.** Asked whether the record shows P014 reviewing
  `ext4: introduce mext_move_extent()` (expected: no), B1 cited `Reviewed-by` from a
  *later* patch that P014 did review and answered **yes**. B3, which never saw that patch,
  quoted the decision's own reviewer line from M0 and answered **no**. The model usually
  ignores future evidence on simple questions; when it does not, the answer is wrong. B3
  removes the risk instead of relying on the model.
- **Gemma's yes/no verdicts are unreliable.** In several presence runs it quoted the right
  `Reviewed-by` line and still answered "no" or "unknown". Treated as a small-model limit;
  the full run will use Gemini.
- Leak terms stayed at zero: "why was this applied?" is answered by the patch message
  itself, so the model had no need for later material. Harder questions are needed to
  measure how often a model uses future evidence it is given.
- A first tools-loop run (presence questions, 3 per policy) showed no future evidence for
  either policy, consistent with Gemma making no tool calls; its numbers are noise.

---

## Known limitations

- **Tier 1 data only.** Commit messages and trailers; the mailing-list discussion behind
  each `Link:` is not ingested yet, so debated alternatives are mostly invisible.
- **Conservative visibility.** Reviewers probably saw patches before they were applied;
  the ledger only credits them from the applied time.
- **Few hindsight test cases.** Only a handful of decisions are later named by a `Fixes:`
  tag. A wider window adds more.
- **Summaries can drift from their quotes** with a small model. Quotes are always shown
  next to summaries.
- **Prefetch can miss evidence.** It reads the 3 most recent same-file patches and the top 3
  search hits; anything further down is not seen. The tools loop exists for that case.
- **Tools loop on Gemini not wired yet.** Gemini runs the prefetch loop only, until a key is
  available to test the tools loop.
- **Small samples.** The model-in-the-loop numbers so far come from 3 questions per type;
  only the exposure contrast (89% vs 0%) is large enough to read on its own.
- **Yes/no verdicts on Gemma** do not reliably follow its own claims.
- **Display of restored names.** A trailer such as `Reviewed-by: P016 <P016>` restores to
  `Jan Kara <Jan Kara>`, because name and email share one placeholder. Cosmetic only; the
  model saw a consistent placeholder.
- **Memorization.** Models may already know kernel history; anonymization reduces this,
  and comparing named and anonymized runs (`--named`) measures it.

---

## Next steps

1. Finish the full extraction run; re-run `local_eval.py retrieval` on the current database.
2. One more attempt at the tools loop on Gemma.
3. Full question set on Gemini, prefetch and tools, B1 and B3; add harder questions where
   later material would change the answer.
4. Wire the tools loop into the Gemini backend.
5. Web interface: decision picker, "as of" time, quotes highlighted against the original
   message, hindsight kept separate, and the evidence trail.
6. Hosted deployment with Gemini.
7. Optional: fetch mailing-list threads from lore.kernel.org for richer alternatives.

---

## Notes

- Keep generated data (`data/`, `eval/`) out of version control.
- Kernel source and commit messages are licensed by their authors; review licensing before
  redistributing diffs or extracted text.
- Research basis: TimeChara (point-in-time character hallucination, ACL Findings 2024) for
  temporal and presence checks; ConStory-Bench (2026) for evidence chains with exact quotes.

# Temporal Consistency

**Explain past decisions using only what the decision-maker could have known at the time.**

Tools that explain why a change was made usually read the whole history, including what
happened afterwards, and so explain a choice with facts nobody had when it was made.
Temporal Consistency reconstructs each decider's **information set** at the moment of the
decision and answers from that evidence alone. Every claim is quoted word for word from its
source, and what happened later is shown separately, as hindsight.

The first dataset is the Linux kernel's ext4 filesystem history (234 decisions, Jan–Oct 2026), where every
event has an exact time, a named author and written reasoning.

**Live demo:** https://software-history-tracker.web.app (9 decisions with prepared answers
from Gemini 3.5 Flash Lite)

---

## Why it matters

A patch applied on 23 Jan 2026 added kunit tests for the ext4 extent status cache. On
28 Mar 2026 a later patch fixed a bug it had introduced. Ask a typical tool why the January
patch was applied and it can mix in the March fix, which the maintainer who applied it never
had. That's unfair to the people who decided, and it makes the explanation wrong.

The obvious fix, a date cutoff, doesn't work either. Measured on all 234 decisions with no
model involved, the share of decisions that would be handed at least one patch the decider
could not yet have seen:

| Evidence allowed | Decisions shown a future patch |
|---|---|
| No time filter | 85% |
| Cutoff on author date | 60% |
| Cutoff on applied date | 0% |
| **Information set** | **0%** |

Kernel patches are often written weeks before they are applied, so an author-date cutoff
admits patches nobody outside the author had seen yet. The same four percentages came out on
three different windows of the history (233, 234 and 287 decisions).

## How it works

```
info_set(person, t) = patches the person took part in, from when they could see each one

answer(decision, question) = model( evidence from info_set(decider, decision time) ONLY )
                             -> claims with quotes; code drops any quote not in the evidence
                             -> hindsight attached afterwards by code, never shown to the model
```

- **A decision** is a patch being applied; **the decider** is the maintainer who applied it.
- **Visibility rule:** an author knows a patch from the day they write it; everyone else only
  from the day it is applied. It can miss early knowledge, but it never credits anyone with
  the future.
- **The boundary lives in the tools, not the prompt.** Code fixes the decision, the decider
  and the time before the model sees anything. The tools (`search_context`, `related_prior`,
  `read_patch`) take no dates or hashes, so the model cannot ask for the future.
- **Every claim quotes its source.** A claim survives only if its quote appears word for word
  in an item the model was actually shown.
- **Names are hidden from the model.** People become `P001`, links `LINK001`, so it cannot
  answer from memory of who they are.
- **Honest answers.** Each answer has a status: `answered`, `not_in_evidence` or
  `premise_false` (for example "Why was this patch rejected?" about a patch that was applied).

Nothing relies on the model behaving, which is why the same guarantee holds on a 4B model on
a laptop and on Gemini.

```
Git history ─► ingest.py ─► SQLite ─► ledger.py ─► extract.py (Gemma, local)
                                          │
                                          ▼
                 agent.py: session ─► bounded tools ─► Gemini / Gemma ─► checks ─► hindsight
                                          │
                                          ▼
                 precompute.py ─► export_static.py ─► React site on Firebase Hosting
```

## Features

- **Decision records:** problem, change and stated reason extracted from each patch as quotes
  (1,052 quotes over 234 decisions, 99% verbatim).
- **Timeline:** what the decider had seen is lit; later events stay in fog until you drag past them.
- **Same question, two answers:** the bounded answer beside one with no boundary, with future
  evidence marked in red.
- **Hindsight column:** later fixes (from `Fixes:` tags) and later changes to the same files.
- **Evidence drawer:** every quote opens its source with the quoted words highlighted.
- **Names as the model saw them:** a toggle to show the anonymised view.

## Results

The full agent on the same questions, with and without the boundary (prefetch loop, 234 decisions):

| Evidence allowed | Model | Runs | Shown a future patch | Quotes verified | Reviewer questions right | False premises caught |
|---|---|---|---|---|---|---|
| No boundary | Gemini 3.8 Flash | 9 | 100% | 100% | 3 of 3 | 3 of 3 |
| **Information set** | Gemini 3.8 Flash | 8 | **0%** | 100% | 3 of 3 | 2 of 2 |
| No boundary | Gemini 3.5 Flash Lite | 8 | 100% | 100% | 3 of 3 | 2 of 2 |
| **Information set** | Gemini 3.5 Flash Lite | 8 | **0%** | 100% | 3 of 3 | 2 of 2 |
| No boundary | Gemma 4 E4B | 9 | 100% | 100% | 1 of 3 | 3 of 3 |
| **Information set** | Gemma 4 E4B | 9 | **0%** | 100% | 1 of 3 | 3 of 3 |

- **Exposure is the headline.** Without the boundary, every run was handed at least one patch
  the decider could not yet have seen, sometimes the decision's own later fix. With the
  information set, none was. This holds on all three models, because code enforces it.
- **The boundary costs nothing in quality.** Quotes, reviewer questions and false premises are
  the same with and without it.
- **Model size matters for yes/no questions.** Both Gemini models answered every reviewer
  question correctly; Gemma, a 4B model, got one in three.
- **No run cited future evidence.** Simple questions are answered from the patch message
  itself, so the models did not need later material. Harder questions are needed to measure
  how often a model *uses* future evidence it is given; the boundary makes that question moot.

**Extraction** (Gemma 4 E4B, all 234 decisions): 1,038 of 1,052 quotes verbatim (99%),
15 decisions needed a retry, 261 duplicate items resolved in code, a stated reason found in
232 of 234 decisions.

**Model-free checks:** the ledger self-test finds 0 leaks across every decision.

Samples are small (3 questions per type per policy). [REPRODUCE.md](REPRODUCE.md) explains
every number and how to get it.

## Quick start

Requirements: Python 3.10+, Git, Node 18+ (for the site), and either
[Ollama](https://ollama.com) with `gemma4:e4b` or a Gemini API key from Google AI Studio.

```bash
# 1. Kernel history, metadata only
git clone --filter=blob:none --single-branch --shallow-since=2025-11-01 \
  https://github.com/torvalds/linux.git data/raw/linux

# 2. Build the database and the ledger
python src/ingest.py --repo data/raw/linux --since 2026-01-01 --path fs/ext4 --db data/db/linux.db
python src/ledger.py build --db data/db/linux.db
python src/ledger.py check --db data/db/linux.db          # expect 0 leaks

# 3. Extract decision records with local Gemma (hours on a laptop CPU; resumable)
ollama pull gemma4:e4b
python src/extract.py run --db data/db/linux.db

# 4. Ask a question
python src/agent.py ask --commit <hash> --question "Why was this patch applied?"
python src/agent.py ask --commit <hash> --question "Why was this patch applied?" --policy B1
```

To use Gemini instead of Gemma:

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...            # PowerShell: $env:GEMINI_API_KEY="..."
python src/agent.py ask --backend gemini --model gemini-3.8-flash --commit <hash> --question "..."
```

### Run the site locally

```bash
python src/precompute.py answers                 # prepared answers for the showcase decisions
python src/precompute.py results                 # evaluation tables for the Results page
uvicorn app:app --app-dir src --port 8000        # API; LIVE_BACKEND=gemini|ollama|off for typed questions
cd web && npm install && npm run dev             # http://localhost:5173, proxies /api to :8000
```

### Deploy as a static site (Firebase Hosting)

```bash
python src/export_static.py                      # writes web/public/data/
cd web && npm run build:static && cd ..
firebase deploy --only hosting
```

The static site serves prepared answers only. Typed questions need the API server, which
also has a `Dockerfile` for Cloud Run.

## Project structure

```
src/
  ingest.py             Git history into SQLite: commits, trailers, Fixes: links, scoped diffs
  ledger.py             People, times, participation; info_set(person, t); leak self-test
  rule_based_checks.py  Anonymiser, verbatim quote check, leak detector
  local_eval.py         Model-free retrieval leak test and question generator
  extract.py            Commit message to a quoted decision record (Ollama, Gemini, fake)
  agent.py              The bounded question-answering agent, evaluation and report
  app.py                FastAPI backend for the site, with a rate-limited live endpoint
  precompute.py         Prepared answers and results for the site
  export_static.py      Static JSON export for Firebase Hosting
web/                    React + Vite front end
firebase.json           Hosting configuration
Dockerfile              Container for the API server
REPRODUCE.md            Step-by-step reproduction and what each number means
```

## Tech stack

- **Models:** Gemini 3.8 Flash and Gemini 3.5 Flash Lite through Google AI Studio (`google-genai`); Gemma 4 E4B, local through Ollama
- **Data:** Git, SQLite, Python standard library
- **Backend:** FastAPI, Uvicorn
- **Frontend:** React 18, Vite 6
- **Hosting:** Firebase Hosting

## Limitations

- **Commit messages only.** The mailing-list discussion behind each patch is not ingested yet,
  so the alternatives people debated are mostly invisible.
- **Conservative visibility.** Reviewers probably saw patches before they were applied; the
  ledger only credits them from the applied date.
- **People are identified by email address.** One person with several addresses counts as
  several people; addresses are not merged yet.
- **Small evaluation samples**, limited by the Gemini free tier (20 requests per day for most
  models). The site's prepared answers use Gemini 3.5 Flash Lite, whose daily limit is higher;
  its answers are shorter than 3.8 Flash's.
- **The tools loop needs a capable model.** Gemma answered without calling its tools, so the
  local default is the prefetch loop, where code makes the same bounded lookups. The tools
  loop on Gemini is not wired yet.
- **Memorisation.** Models may already know kernel history; anonymisation reduces this, and
  `--named` runs measure it.

## Roadmap

- Mailing-list threads from lore.kernel.org, for the alternatives people debated
- A larger evaluation, and the tools loop on Gemini
- Live questions on the hosted site
- Historical archives: letters, minutes and telegrams, where who knew what, and when, is the
  central question

## Notes

- Generated data (`data/`, `eval/`, `web/public/data/`) is not committed.
- Kernel commit messages are the work of their authors; check licensing before
  redistributing extracted text.
- Research basis: TimeChara (point-in-time character hallucination, ACL Findings 2024);
  ConStory-Bench (2026) for evidence chains with exact quotes.

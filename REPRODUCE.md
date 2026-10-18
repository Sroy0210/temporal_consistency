# Reproducing the results

A step-by-step guide from an empty machine to the numbers in the README, with what each
number means. Everything up to step 8 runs locally with no cloud account; step 9 uses a
free Gemini API key, and step 10 builds and deploys the site.

Commands are shown for Windows PowerShell; they work the same in bash except where noted.

---

## 0. Requirements

| Item | Version used | Notes |
|---|---|---|
| Python | 3.13 (3.10+ should work) | Pipeline uses the standard library; `requirements.txt` adds `google-genai`, FastAPI and Uvicorn |
| Node | 18 or later | Only for the web front end (step 10) |
| Git | any recent | For the kernel clone |
| SQLite command line | optional | Only for the inspection queries below |
| Ollama | 0.30.9 or later | Runs the local model |
| Model | `gemma4:e4b` (Q4_K_M) | Several GB download |
| Machine | 16 GB RAM, 8-core laptop CPU | Measured on a Ryzen 7 7735U; no GPU used |
| Disk | a few GB | Mostly the blob-less kernel clone |

Project layout expected by the commands:

```
project/
  src/       ingest.py  ledger.py  rule_based_checks.py  local_eval.py  extract.py  agent.py
             app.py  precompute.py  export_static.py
  web/       (React front end)
  data/raw/  (kernel clone)
  data/db/   (SQLite database, created by step 2)
  data/demo/ (prepared answers and results, created by step 10)
  eval/      (generated questions)
```

Run every command from `project/`.

**PowerShell notes:** use `curl.exe` and `fc.exe`, not `curl` and `fc` (those are other
commands in PowerShell). Set environment variables with `$env:NAME="value"`.

---

## 1. Clone the kernel history

```powershell
git clone --filter=blob:none --single-branch --shallow-since=2025-11-01 https://github.com/torvalds/linux.git data/raw/linux
```

- `--filter=blob:none` downloads commit metadata only; file contents are fetched on demand
  when a diff is needed.
- The clone starts two months before the analysis window because kernel patches are based on
  older releases; starting later cuts off their parents and creates false "boundary" commits.
- If it fails with "early EOF": `git config --global http.version HTTP/1.1` and retry, or
  clone a smaller range and deepen in steps.

## 2. Ingest ext4

```powershell
python src/ingest.py --repo data/raw/linux --since 2026-01-01 --path fs/ext4 --db data/db/linux.db
```

- Takes under a minute with diffs. `--no-diffs` makes it faster.
- Expect a summary with the commit count and "Fixes: tags resolved to commits in the DB".
- The README's results come from a fresh clone on 18 Oct 2026: 234 decisions, 113 people,
  7 decisions with a later fix inside the window. A later clone will have more commits; the
  retrieval percentages were the same on 233, 234 and 287 decisions.
- `--reset` rebuilds a database made by an older version of the ingester
  (the error "no such column repo_id" means you need it).

## 3. Build and check the ledger

```powershell
python src/ledger.py build --db data/db/linux.db
python src/ledger.py check --db data/db/linux.db
```

Expected: `checked N decisions x 2 modes: 0 leaks; K decisions have a later fix`.

Read one decision by hand and compare it with Git:

```powershell
python src/ledger.py inspect --db data/db/linux.db --commit 82f80e2e3b23
git -C data/raw/linux show --stat 82f80e2e3b23 -- fs/ext4
```

People are numbered in order of email address, so the same database always gives the same
numbers. **People are identified by email:** someone who committed under three addresses gets
three numbers (for example P064, P065 and P066 for one person). If the database changes,
regenerate the question file (step 4), or the questions may name the wrong people.

## 4. Model-free evaluation

```powershell
python src/local_eval.py selftest  --db data/db/linux.db
python src/local_eval.py retrieval --db data/db/linux.db
python src/local_eval.py questions --db data/db/linux.db --out eval/questions.jsonl
```

- `selftest`: the anonymizer leaves no email addresses, the quote check accepts a real
  sentence and rejects an invented one, the leak detector flags a later fix.
- `retrieval`: the retrieval leak table (section "What the numbers mean").
- `questions`: writes test questions whose answers come from metadata (fixed random seed, so
  the same database gives the same questions).

## 5. Local model

```powershell
ollama pull gemma4:e4b
ollama ps                       # after the first call, shows the model loaded, 100% CPU
curl.exe http://localhost:11434 # "Ollama is running"
```

The Windows app keeps Ollama running in the background. For long runs keep the laptop
plugged in, set the power mode to best performance, and stop it sleeping (including on lid close).

## 6. Decision extraction

```powershell
python src/extract.py run   --db data/db/linux.db --limit 20
python src/extract.py stats --db data/db/linux.db
python src/extract.py review --db data/db/linux.db --limit 5
python src/extract.py run   --db data/db/linux.db              # all remaining, overnight
```

- 20 decisions take 15-45 minutes; the full set several hours.
- Results are cached per commit, model and prompt version; re-running continues where it stopped.
- A call that times out skips that decision; the next run retries it.

## 7. Agent

Do not run this while the extraction is running; both use the whole CPU.

```powershell
python src/agent.py selftest --db data/db/linux.db
```

Expected: every line `PASS`, ending `all checks passed`.

Single questions (about 20-100 s each):

```powershell
python src/agent.py ask --commit 82f80e2e3b23 --question "Why was this patch applied?"
python src/agent.py ask --commit 82f80e2e3b23 --question "Why was this patch applied?" --policy B1
python src/agent.py ask --commit 82f80e2e3b23 --question "Why did the maintainer reject this patch?"
```

Reviewer (presence) questions, B1 against B3. Pick any decision, then a person, and ask
about them by number. Use the number whose email appears on the patch, since one person can
have several:

```powershell
sqlite3 data/db/linux.db "SELECT commit_hash, subject FROM commits WHERE subject LIKE '%<words from the subject>%';"
sqlite3 data/db/linux.db "SELECT printf('P%03d', actor_id), name, email FROM actors WHERE name LIKE '%<name>%';"
python src/agent.py ask --commit <hash> --policy B1 --question "Does the record show P0xx reviewing or acking '<subject>' before it was applied?"
python src/agent.py ask --commit <hash> --policy B3 --question "Does the record show P0xx reviewing or acking '<subject>' before it was applied?"
```

Look for `[FUTURE EVIDENCE]` on a B1 claim: the model cited a patch the decider could not
yet have seen. A person who reviewed many later patches is the most likely to trigger it.
`No commit matches '<hash>'` means that commit is not in your database; find it by subject.

Tools loop (the model chooses the lookups; Gemma has not made tool calls so far):

```powershell
python src/agent.py ask --commit 82f80e2e3b23 --question "What earlier work does this patch build on?" --loop tools
python src/agent.py ask --commit 82f80e2e3b23 --question "What earlier work does this patch build on?" --loop tools --think-tools
```

## 8. Evaluation

```powershell
python src/agent.py eval   --db data/db/linux.db --loop prefetch --policy B1,B3 --per-type 3
python src/agent.py report --db data/db/linux.db
```

- 3 questions per type x 3 types x 2 policies = 18 runs, about 20-30 minutes on Gemma.
- Questions are sampled with a fixed seed, so the same question file gives the same sample.
- Saved runs are skipped on re-run. After changing question wording or the prompt, delete the
  affected rows first, for example `sqlite3 data/db/linux.db "DELETE FROM agent_runs WHERE qtype='presence';"`.
- Inspect individual answers:
  `sqlite3 data/db/linux.db "SELECT policy, expected, verdict, json_extract(result_json,'$.claims') FROM agent_runs WHERE qtype='presence';"`

## 9. Gemini

Get a free API key from Google AI Studio. No billing account is needed.

```powershell
pip install -r requirements.txt
$env:GEMINI_API_KEY="..."
python src/agent.py ask  --backend gemini --model gemini-3.8-flash --commit 82f80e2e3b23 --question "Why was this patch applied?"
python src/agent.py eval --backend gemini --model gemini-3.8-flash --loop prefetch --policy B1,B3 --per-type 3
python src/agent.py report
```

- Gemini runs the prefetch loop only for now.
- **Free-tier limits are per model** (check ai.dev/rate-limit): most Flash models allow 5
  requests per minute and 20 per day; Gemini 3.5 Flash Lite allowed 15 per minute and 500 per
  day, which is why the site's 64 prepared answers use it. An answer that needs a retry uses
  two requests, so one evaluation pass on a 20-a-day model may take more than a day. Saved runs are skipped, so re-run the
  same `eval` command the next day and it continues.
- `503 UNAVAILABLE` means the model is briefly overloaded; re-run. `429 RESOURCE_EXHAUSTED`
  means the daily quota is used up.
- Never commit the key. `.env` files are ignored by Git.

## 10. Web interface and deployment

Prepared answers for the showcase decisions, then the results tables:

```powershell
python src/precompute.py answers                          # Gemma by default
python src/precompute.py answers --commit <hash>          # add a decision to the showcase
python src/precompute.py results
```

Run locally (two terminals):

```powershell
uvicorn app:app --app-dir src --port 8000
cd web; npm install; npm run dev                          # http://localhost:5173
```

Typed questions are off unless `LIVE_BACKEND` is set to `gemini` (with `GEMINI_API_KEY` and
`GEMINI_MODEL`) or `ollama`. Live questions are rate-limited (`LIVE_PER_HOUR`, `LIVE_PER_IP`).

Static site on Firebase Hosting (free plan, no billing):

```powershell
python src/export_static.py                               # writes web/public/data/
cd web; npm run build:static; cd ..
firebase deploy --only hosting
```

Run these from the project folder, which holds `firebase.json` and `.firebaserc`. Rebuild
and redeploy after every `precompute.py` run, or the site keeps showing the old answers.

---

## What the numbers mean

### Ledger check (step 3)

| Output | Meaning | Good value |
|---|---|---|
| `0 leaks` | An independent recomputation found no patch in any information set that the decider could not yet have seen | Exactly 0 |
| `K decisions have a later fix` | Decisions named by a later `Fixes:` tag inside the database; these are the hindsight test cases | More is better |

The check guards the query against future edits. It cannot catch a rule that is wrong in
both places, which is why step 3 also reads one decision by hand.

### Retrieval leak table (step 4)

For every decision, the decision's own text is used as a search query, and four setups each
pick the top 8 earlier patches a model would be given. No model is involved.

| Column | Meaning |
|---|---|
| Contexts containing a future patch | Share of decisions where at least one of the 8 patches was not yet visible to the decider |
| Later fix in context | Of the decisions that were later fixed, how many had that fix among their 8 patches |
| Empty contexts | Decisions where the setup found nothing to give the model |

| Setup | Allowed patches |
|---|---|
| No time filter (B1) | Everything in the database |
| Cutoff on author date (B2a) | Patches *written* before the decision |
| Cutoff on applied date (B2b) | Patches *applied* before the decision (public at the time) |
| Information set (B3) | Patches the decider took part in, from when they could have seen them |

Reading it: author-date cutoffs look safe but still expose future material, because kernel
patches are often written weeks or months before they are applied. B3's empty contexts are
decisions where the decider had no prior involvement; the right answer there is "not in
evidence".

### Extraction stats (step 6)

| Field | Meaning | Good value |
|---|---|---|
| quotes kept X of Y | Items whose quote is verbatim in the commit message | Close to 100% |
| needed retry | Decisions where the first answer had an invalid quote and was asked again | Low |
| duplicates dropped | Items removed because their quote repeated (or sat inside) a quote in a higher-priority field | Informational; rationale keeps ties |
| rationale stated | Decisions where the model says the message gives a reason | Should match decisions that have a rationale item |
| s, output tokens each | Average time and output length per decision | Hardware dependent |
| items per field | Counts across all decisions | Low alternatives/concerns are expected from commit messages alone |

### Agent answer (step 7)

| Part | Meaning |
|---|---|
| `N earlier patches allowed` | Size of the allowed set under this policy |
| STATUS | `answered`; `not_in_evidence` (the evidence does not say; a correct answer); `premise_false` (the question assumes something the record contradicts) |
| VERDICT | For yes/no questions: `yes`, `no`, `unknown`; otherwise `n/a` |
| Claims, `[D0]` `[M0]` `[E3]` | Each claim's verbatim quote and where it came from: the patch message, the fact sheet, or an earlier patch |
| `[FUTURE EVIDENCE]` | The cited patch was not yet visible to the decider (possible under B1 and B2a only) |
| REJECTED | Claims removed by the checks: quote not verbatim, or source not shown to the model |
| WITH HINDSIGHT | Later fixes and later patches to the same files, added by code; never shown to the model |
| CHECKS | Claims kept and rejected; future citations; leak terms in raw output; refused tool calls; attempts; time |
| TRACE | The lookups made: `prefetch()` (code chose them) or individual tool calls (the model chose them) |

### Evaluation report (step 8)

| Column | How it is computed | What it tells you |
|---|---|---|
| runs | Saved answers in this group | Sample size; read every other column against it |
| future shown | Runs where any evidence item given to the model was not yet visible to the decider | **Exposure.** What the retrieval boundary let through. B3 should be exactly 0% |
| quotes valid | Claims kept / claims made | Whether answers are grounded in real text |
| cited future | Runs, any question type, with a surviving claim citing a patch not yet visible | **Use.** How often the model actually relied on future evidence it was given |
| future leak | Future-leak questions only: a later fix's hash or subject in the raw output, or a future citation | Whether hindsight about this specific decision got into the answer |
| presence right | Yes/no verdict equals the record | Whether the model reads its own evidence correctly |
| fake premise caught | "Why was it rejected?" answered `premise_false` | Whether the model resists a false premise instead of inventing reasons |
| no evidence | Answers with status `not_in_evidence` | Honest abstention; too high means retrieval is missing evidence |
| avg s | Mean seconds per answer | Hardware and loop dependent |

**How to read the current results:** exposure (future shown) is 100% without the boundary and
0% with the information set on all three models (Gemini 3.8 Flash, Gemini 3.5 Flash Lite,
Gemma 4 E4B); that is the retrieval boundary at work and is large enough to read at 8 or 9
runs per policy. Use (cited future) was 0 everywhere, because simple questions are answered
from the patch message itself. Reviewer questions: both Gemini models 3/3 under each policy,
Gemma 1/3. The remaining columns are smoke tests at 3 questions per type.

### Why your numbers may differ slightly

- **Different window or clone depth:** changes decision counts, people's numbers and the questions.
- **Different model build or Ollama version:** answers can change even at temperature 0.
- **CPU timing:** timings vary with load, heat and power mode; answers should not.
- **Small samples:** one changed answer moves a 3-question column by 33 points.
- **Gemini model versions:** a hosted model can change behind the same ID.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `early EOF` during clone or fetch | Network drop on a large transfer: `git config --global http.version HTTP/1.1`, retry, deepen in smaller steps |
| `no such column repo_id` | Database from an older ingester: add `--reset`, or use a new `--db` |
| Ingest looks stuck | A treewide commit is fetching blobs; per-commit progress is printed, give it time or use `--no-diffs` |
| `Cannot reach Ollama` | Start the Ollama app; check `curl.exe http://localhost:11434` |
| Extraction `TimeoutError` | The laptop slept or the CPU was busy; re-run, it continues |
| Very slow runs | On battery, power-saver mode, or another heavy process; check Task Manager |
| `ModuleNotFoundError` | A file was renamed but an import was not; imports must match `rule_based_checks` and `local_eval` |
| Presence questions name the wrong person | The ledger was rebuilt after the questions were generated; regenerate questions |
| `Format-Custom : A positional parameter...` | PowerShell `fc`; use `fc.exe` or `Compare-Object` |
| `No commit matches '<hash>'` | The commit is not in your database (different window or clone); look it up by subject |
| Gemini `429 RESOURCE_EXHAUSTED` | Free-tier daily quota used; continue the next day, saved runs are skipped |
| Gemini `503 UNAVAILABLE` | Model briefly overloaded; re-run |
| `client has been closed` (google-genai) | Keep the client in a variable instead of calling it inline |
| `Missing script: "build:static"` | `web/package.json` is missing the script, or `web/.env.static` is missing |
| `Directory 'web/dist' ... does not exist` | Run `npm run build:static` before `firebase deploy` |
| `must be run from a Firebase project directory` | Run Firebase commands from the folder that holds `firebase.json` |

#!/usr/bin/env python3
"""Prepare what the web interface serves without calling a model.

  python src/precompute.py list                               # showcase decisions and their questions
  python src/precompute.py answers --backend ollama           # answer them with local Gemma (slow; resumable)
  python src/precompute.py answers --backend gemini --model <id>
  python src/precompute.py results                            # retrieval and evaluation tables

Output goes to data/demo/: answers.json (one entry per decision, policy and question) and
results.json. The site serves these instantly, so the demo works with no model and no key.

Showcase decisions: every decision with a later fix, plus decisions the decider had no
earlier involvement in, plus any --commit you add. For each, the same questions are asked
under B3 (information set) and B1 (no time filter), so the site can compare them.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import time
from pathlib import Path

from agent import FakeAgent, GeminiAgent, OllamaAgent, answer
from ledger import decision, hindsight, info_set
from local_eval import FTS_SCHEMA, K, all_decisions, baselines, seen_time
from rule_based_checks import Anonymizer

BASE_QUESTIONS = ["Why was this patch applied?", "Why did the maintainer reject this patch?"]
FIXED_QUESTION = "Was this a sound decision, given what was known at the time?"
DEFAULT_POLICIES = ("B3", "B1")


def demo_key(commit: str, policy: str, loop: str, question: str) -> str:
    return f"{commit}|{policy}|{loop}|{question.strip()}"


# ---------------------------------------------------------------- showcase selection

def trap_reviewer(conn, h: str) -> str | None:
    """Someone who reviewed a LATER patch to the same files but not this one.

    Asking whether they reviewed this patch is the case where later evidence misleads:
    an unfiltered model sees their Reviewed-by on the later patch.
    """
    d = decision(conn, h)
    mine = {r[0] for r in conn.execute(
        "SELECT actor_id FROM participation WHERE commit_hash=? AND role IN ('reviewed_by','acked_by')", (h,))}
    for later, _, _ in hindsight(conn, h, d["applied"])["later_same_files"]:
        for aid, name in conn.execute("""SELECT p.actor_id, a.name FROM participation p JOIN actors a USING (actor_id)
                WHERE p.commit_hash=? AND p.role IN ('reviewed_by','acked_by')""", (later,)):
            if aid not in mine and aid != d["decider"] and name:
                return name
    return None


def showcase(conn, extra: list[str], n_empty: int = 2) -> list[dict]:
    fixed = [r[0] for r in conn.execute(
        "SELECT DISTINCT fixed_commit FROM fix_links WHERE fixed_commit IS NOT NULL")]
    empty = []
    for (h,) in conn.execute("SELECT commit_hash FROM commit_times ORDER BY applied_utc"):
        d = decision(conn, h)
        if h not in fixed and not info_set(conn, d["decider"], d["applied"], "knew", exclude=h):
            empty.append(h)
    random.Random(7).shuffle(empty)
    chosen = list(dict.fromkeys(fixed + empty[:n_empty] + [decision(conn, c)["hash"] for c in extra]))
    out = []
    for h in chosen:
        d = decision(conn, h)
        qs = list(BASE_QUESTIONS)
        if h in fixed:
            qs.append(FIXED_QUESTION)
            who = trap_reviewer(conn, h)
            if who:
                qs.append(f"Does the record show {who} reviewing or acking '{d['subject']}' before it was applied?")
        out.append(dict(commit=h, subject=d["subject"], later_fixed=h in fixed, questions=qs))
    return out


# ---------------------------------------------------------------- results tables

def retrieval_rows(conn) -> dict:
    """Same measurement as `local_eval.py retrieval`, returned as data."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='commit_fts'").fetchone():
        conn.executescript(FTS_SCHEMA)
    fixed = {r[0] for r in conn.execute("SELECT fixed_commit FROM fix_links WHERE fixed_commit IS NOT NULL")}
    stats: dict[str, dict[str, int]] = {}
    decisions = all_decisions(conn)
    for d in decisions:
        later = {r[0] for r in conn.execute("SELECT fixing_commit FROM fix_links WHERE fixed_commit=?", (d["hash"],))}
        for name, rows in baselines(conn, d).items():
            s = stats.setdefault(name, {"future": 0, "fix": 0, "empty": 0, "rows": 0})
            s["rows"] += len(rows)
            s["empty"] += not rows
            s["future"] += any(seen_time(conn, r, d["decider"]) > d["applied"] for r in rows)
            if d["hash"] in fixed:
                s["fix"] += bool(later & set(rows))
    n = len(decisions)
    return dict(decisions=n, later_fixed=len(fixed), top_k=K, rows=[
        dict(setup=name, future_pct=round(100 * s["future"] / n) if n else 0, later_fix_in_context=s["fix"],
             empty=s["empty"], avg_rows=round(s["rows"] / n, 1) if n else 0) for name, s in stats.items()])


def eval_rows(conn) -> list[dict]:
    """The `agent.py report` table, returned as data."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='agent_runs'").fetchone():
        return []
    rows = conn.execute("""SELECT policy, loop, model, qtype, expected, status, verdict, claims, rejected,
        leaks_raw, future_cites, seconds, commit_hash, result_json FROM agent_runs WHERE qtype IS NOT NULL""").fetchall()
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault(r[:3], []).append(r)
    out = []
    for (pol, loop, model), rs in sorted(groups.items()):
        def frac(sel, cond):
            sel = list(sel)
            return [sum(1 for r in sel if cond(r)), len(sel)]
        shown = 0
        for r in rs:
            d = decision(conn, r[12])
            ev = json.loads(r[13]).get("evidence_ids", {})
            shown += any(seen_time(conn, h, d["decider"]) > d["applied"] for i, h in ev.items() if i.startswith("E"))
        kept, rej = sum(r[7] for r in rs), sum(r[8] for r in rs)
        out.append(dict(
            policy=pol, loop=loop, model=model, runs=len(rs),
            future_shown=[shown, len(rs)], quotes_valid=[kept, kept + rej],
            cited_future=frac(rs, lambda r: r[10] > 0),
            future_leak=frac((r for r in rs if r[3] == "future_leak"), lambda r: r[9] > 0 or r[10] > 0),
            presence_right=frac((r for r in rs if r[3] == "presence"), lambda r: r[6] == r[4]),
            fake_premise_caught=frac((r for r in rs if r[3] == "fake_premise"), lambda r: r[5] == "premise_false"),
            no_evidence=frac(rs, lambda r: r[5] == "not_in_evidence"),
            avg_seconds=round(sum(r[11] or 0 for r in rs) / len(rs), 1)))
    return out


# ---------------------------------------------------------------- CLI

def make_backend(args):
    if args.backend == "fake":
        return FakeAgent()
    if args.backend == "gemini":
        if not args.model:
            raise SystemExit("--backend gemini needs --model <a Gemini model id>")
        return GeminiAgent(args.model)
    return OllamaAgent(args.model or "gemma4:e4b", args.host)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)                       # never leaves a half-written file behind


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["list", "answers", "results"])
    p.add_argument("--db", default="data/db/linux.db")
    p.add_argument("--out", default="data/demo")
    p.add_argument("--commit", action="append", default=[], help="add a showcase decision; repeatable")
    p.add_argument("--policies", default=",".join(DEFAULT_POLICIES))
    p.add_argument("--loop", choices=["prefetch", "tools"], default="prefetch")
    p.add_argument("--backend", choices=["ollama", "gemini", "fake"], default="ollama")
    p.add_argument("--model")
    p.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    p.add_argument("--redo", action="store_true", help="answer again even if an answer is saved")
    p.add_argument("--only", action="append", default=[], help="limit the showcase to these commits; repeatable")
    a = p.parse_args()
    conn = sqlite3.connect(a.db, timeout=30)
    out = Path(a.out)

    if a.action == "results":
        data = dict(retrieval=retrieval_rows(conn), evaluation=eval_rows(conn), built_at=time.strftime("%Y-%m-%d %H:%M"))
        write_json(out / "results.json", data)
        print(f"wrote {out / 'results.json'}: {len(data['retrieval']['rows'])} retrieval rows, "
              f"{len(data['evaluation'])} evaluation rows")
        return

    cases = showcase(conn, a.commit)
    if a.only:
        cases = [c for c in cases if any(c["commit"].startswith(x) for x in a.only)]
    if a.action == "list":
        for c in cases:
            print(f"{c['commit'][:12]}  {'later fixed  ' if c['later_fixed'] else 'no prior work'}  {c['subject'][:60]}")
            for q in c["questions"]:
                print(f"      {q}")
        print(f"\n{len(cases)} decisions, {sum(len(c['questions']) for c in cases)} questions x "
              f"{len(a.policies.split(','))} policies")
        return

    path = out / "answers.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"items": {}}
    backend, anon = make_backend(a), Anonymizer(conn)
    todo = [(c, q, pol) for c in cases for q in c["questions"] for pol in a.policies.split(",")]
    print(f"{len(todo)} answers to prepare with {backend.name} ({a.loop})", flush=True)
    for i, (c, q, pol) in enumerate(todo, 1):
        k = demo_key(c["commit"], pol, a.loop, q)
        if k in saved["items"] and not a.redo:
            continue
        if a.backend == "gemini":
            time.sleep(13)
        try:
            r = answer(conn, c["commit"], q, backend, pol, a.loop, anon=anon)
        except ConnectionError as e:
            raise SystemExit(f"ERROR: {e}")
        except Exception as e:                       # one failure must not lose the others
            print(f"  [{i}/{len(todo)}] {c['commit'][:12]} {pol} FAILED {type(e).__name__}: {e}", flush=True)
            continue
        saved["items"][k] = r
        saved["showcase"] = cases
        write_json(path, saved)
        print(f"  [{i}/{len(todo)}] {c['commit'][:12]} {pol:3} {r['status']:15} claims {len(r['claims'])} "
              f"future {r['future_cites']}  {r['seconds']}s  {q[:50]}", flush=True)
    saved["showcase"] = cases
    write_json(path, saved)
    print(f"done: {len(saved['items'])} answers in {path}")


if __name__ == "__main__":
    main()

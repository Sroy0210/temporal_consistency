from __future__ import annotations

import re
import random
import json
import argparse
import sqlite3
from pathlib import Path

from ledger import decision, info_set
from rule_based_checks import Anonymizer, verify_quote, contains_leak, leak_terms

K = 8   # rows retrieved per question
 
FTS_SCHEMA = """
DROP TABLE IF EXISTS commit_fts;
CREATE VIRTUAL TABLE commit_fts USING fts5(commit_hash UNINDEXED, subject, body);
INSERT INTO commit_fts SELECT c.commit_hash, c.subject, c.body
  FROM commits c JOIN commit_times USING (commit_hash);
"""

def fts_query(text: str) -> str:
    """Turn free text into an FTS5 OR-query of its distinctive words."""
    words = {w for w in re.findall(r"[a-z_][a-z0-9_]{3,}", text.lower())
             if w not in {"ext4", "this", "that", "with", "from", "when", "which", "have", "should"}}
    return " OR ".join(sorted(words)[:30]) or "ext4"
 
 
def ranked(conn, query: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT commit_hash FROM commit_fts WHERE commit_fts MATCH ? ORDER BY bm25(commit_fts) LIMIT 400",
        (fts_query(query),))]
 
 
def baselines(conn, d: dict) -> dict[str, list[str]]:
    """Top-K rows each retrieval setup would hand the model for this decision."""
    t, me = d["applied"], d["hash"]
    hits = [h for h in ranked(conn, d["subject"] + " " + d["body"][:600]) if h != me]
    times = dict((h, (a, ap)) for h, a, ap in conn.execute(
        "SELECT commit_hash, authored_utc, applied_utc FROM commit_times"))
    knew = {r[0] for r in info_set(conn, d["decider"], t, "knew", exclude=me)}
    return {
        "B1 no time filter":          hits[:K],
        "B2a cutoff on author date":  [h for h in hits if times[h][0] <= t][:K],
        "B2b cutoff on applied date": [h for h in hits if times[h][1] <= t][:K],
        "B3 information set":         [h for h in hits if h in knew][:K],
    }
 
 
def seen_time(conn, row: str, actor: int) -> int:
    """When the actor could first have seen this row (same rule as ledger.info_set)."""
    a, ap = conn.execute("SELECT authored_utc, applied_utc FROM commit_times WHERE commit_hash=?",
                         (row,)).fetchone()
    is_author = conn.execute("SELECT 1 FROM participation WHERE commit_hash=? AND actor_id=? AND role='author'",
                             (row, actor)).fetchone()
    return a if is_author else ap
 
 
def all_decisions(conn) -> list[dict]:
    out = []
    for (h,) in conn.execute("SELECT commit_hash FROM commit_times ORDER BY applied_utc"):
        d = decision(conn, h)
        d["body"] = conn.execute("SELECT body FROM commits WHERE commit_hash=?", (h,)).fetchone()[0] or ""
        out.append(d)
    return out
 
 
# ---------------------------------------------------------------- retrieval
 
def retrieval(conn) -> None:
    conn.executescript(FTS_SCHEMA)
    fixed = {r[0] for r in conn.execute("SELECT fixed_commit FROM fix_links WHERE fixed_commit IS NOT NULL")}
    stats: dict[str, dict[str, int]] = {}
    decisions = all_decisions(conn)
    for d in decisions:
        later_fixes = {r[0] for r in conn.execute(
            "SELECT fixing_commit FROM fix_links WHERE fixed_commit=?", (d["hash"],))}
        for name, rows in baselines(conn, d).items():
            s = stats.setdefault(name, {"future": 0, "fix": 0, "empty": 0, "rows": 0})
            s["rows"] += len(rows)
            s["empty"] += not rows
            s["future"] += any(seen_time(conn, r, d["decider"]) > d["applied"] for r in rows)
            if d["hash"] in fixed:
                s["fix"] += bool(later_fixes & set(rows))
    n, nf = len(decisions), len(fixed)
    print(f"{n} decisions, top {K} rows each; {nf} decisions have a later fix\n")
    print(f"{'setup':28} {'any future row':>15} {'later fix in context':>21} {'empty context':>14} {'avg rows':>9}")
    for name, s in stats.items():
        print(f"{name:28} {100*s['future']/n:14.0f}% {s['fix']:>11} of {nf:<7} {s['empty']:>14} {s['rows']/n:9.1f}")
 
 
# ---------------------------------------------------------------- questions
 
def questions(conn, out: Path) -> None:
    """Test questions with answers taken from metadata, so no labelling is needed."""
    rnd = random.Random(7)
    actors = [r[0] for r in conn.execute("""SELECT actor_id FROM participation
        WHERE role IN ('reviewed_by','acked_by') GROUP BY actor_id HAVING COUNT(*) >= 3""")]
    qs = []
    for d in all_decisions(conn):
        h, t = d["hash"], d["applied"]
        terms = leak_terms(conn, h, t)
        if terms:
            qs.append(dict(type="future_leak", commit=h, t=t,
                           question=f"Why was the patch '{d['subject']}' applied, and was it a sound decision?",
                           forbidden=terms))
        reviewers = {aid for aid, in conn.execute(
            "SELECT actor_id FROM participation WHERE commit_hash=? AND role IN ('reviewed_by','acked_by')", (h,))}
        if reviewers:
            r = rnd.choice(sorted(reviewers))
            qs.append(dict(type="presence", commit=h, t=t, actor=r, answer="yes",
                           question=f"Does the record show P{r:03d} reviewing or acking '{d['subject']}' before it was applied?"))
            others = [a for a in actors if a not in reviewers and a != d["decider"]]
            if others:
                o = rnd.choice(others)
                qs.append(dict(type="presence", commit=h, t=t, actor=o, answer="no",
                               question=f"Does the record show P{o:03d} reviewing or acking '{d['subject']}' before it was applied?"))
        qs.append(dict(type="fake_premise", commit=h, t=t, answer="it was applied, not rejected",
                       question=f"Why did the maintainer reject the patch '{d['subject']}'?"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(q) for q in qs) + "\n")
    by = {}
    for q in qs:
        by[q["type"]] = by.get(q["type"], 0) + 1
    print(f"wrote {len(qs)} questions to {out}: {by}")
 
 
# ---------------------------------------------------------------- self-test
 
def selftest(conn) -> None:
    anon = Anonymizer(conn)
    h, body = conn.execute("""SELECT commit_hash, body FROM commits JOIN commit_times USING (commit_hash)
        WHERE body LIKE '%Reviewed-by:%' LIMIT 1""").fetchone()
    hidden = anon.hide(body)
    leftover = [e for e in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", hidden)]
    print(f"anonymizer: {len(anon.back)} people; emails left after hiding one message: {leftover or 'none'}")
    print(f"  round trip restores text: {anon.show(hidden).lower() == body.lower() or 'names differ in case only'}")
    sample = body.split("\n\n")[0][:80]
    print(f"quote check: real sentence -> {verify_quote(sample, body)}; "
          f"invented -> {verify_quote('this patch was rejected by the maintainer', body)}")
    d = conn.execute("SELECT fixed_commit FROM fix_links WHERE fixed_commit IS NOT NULL LIMIT 1").fetchone()
    if d:
        t = conn.execute("SELECT applied_utc FROM commit_times WHERE commit_hash=?", d).fetchone()[0]
        terms = leak_terms(conn, d[0], t)
        print(f"leak terms for {d[0][:12]}: {terms}")
        print(f"  answer quoting the fix flagged: {bool(contains_leak('It was later fixed: ' + terms[1], terms))}; "
              f"clean answer flagged: {bool(contains_leak('It adds test coverage.', terms))}")

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["retrieval", "questions", "selftest"])
    p.add_argument("--db", type=Path, default=Path("data/db/linux.db"))
    p.add_argument("--out", type=Path, default=Path("eval/questions.jsonl"))
    a = p.parse_args()
    conn = sqlite3.connect(a.db)
    {"retrieval": lambda: retrieval(conn), "questions": lambda: questions(conn, a.out),
        "selftest": lambda: selftest(conn)}[a.action]()

if __name__ == "__main__":
    main()

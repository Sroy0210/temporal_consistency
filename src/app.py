"""Web backend: the decision explorer API, plus the built React app.

  uvicorn app:app --app-dir src --port 8000          (run from the project folder)

Answers come from two places:
  prepared   data/demo/answers.json, made by precompute.py; served instantly, no model needed
  live       a model called per request; off unless configured below

Environment:
  DB_PATH          SQLite database                        (default data/db/linux.db)
  DEMO_DIR         prepared answers and results            (default data/demo)
  WEB_DIST         built frontend (npm run build)          (default web/dist)
  LIVE_BACKEND     off | gemini | ollama | fake            (default: gemini if GEMINI_API_KEY and
                                                            GEMINI_MODEL are set, otherwise off)
  GEMINI_API_KEY, GEMINI_MODEL                             for live answers with Gemini
  OLLAMA_MODEL, OLLAMA_HOST                                for live answers with local Gemma
  LIVE_PER_HOUR    live answers per hour, all visitors     (default 30)
  LIVE_PER_IP      live answers per hour, one visitor      (default 8)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import POLICIES, FakeAgent, GeminiAgent, NoAnon, OllamaAgent, Session, answer
from ledger import decision, files_of, hindsight, info_set, iso
from local_eval import FTS_SCHEMA, seen_time
from precompute import demo_key, eval_rows, retrieval_rows
from rule_based_checks import Anonymizer

DB_PATH = os.environ.get("DB_PATH", "data/db/linux.db")
DEMO_DIR = Path(os.environ.get("DEMO_DIR", "data/demo"))
WEB_DIST = Path(os.environ.get("WEB_DIST", "web/dist"))
LIVE_PER_HOUR = int(os.environ.get("LIVE_PER_HOUR", "30"))
LIVE_PER_IP = int(os.environ.get("LIVE_PER_IP", "8"))
FIELDS = ("problem", "change", "rationale", "context", "alternatives", "concerns")


def db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)


def make_live_backend():
    choice = os.environ.get("LIVE_BACKEND") or (
        "gemini" if os.environ.get("GEMINI_API_KEY") and os.environ.get("GEMINI_MODEL") else "off")
    if choice == "gemini":
        return GeminiAgent(os.environ["GEMINI_MODEL"])
    if choice == "ollama":
        return OllamaAgent(os.environ.get("OLLAMA_MODEL", "gemma4:e4b"),
                           os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    if choice == "fake":
        return FakeAgent()
    return None


class Limiter:
    """Caps live model calls: a guard on cost, not on abuse in general."""

    def __init__(self):
        self.all: deque = deque()
        self.ip: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def take(self, ip: str, n: int = 1) -> None:
        now = time.time()
        with self.lock:
            for q in (self.all, self.ip[ip]):
                while q and now - q[0] > 3600:
                    q.popleft()
            if len(self.all) + n > LIVE_PER_HOUR or len(self.ip[ip]) + n > LIVE_PER_IP:
                raise HTTPException(429, "The hourly limit for live answers is reached. "
                                         "Prepared questions still work; try a live question later.")
            for _ in range(n):
                self.all.append(now)
                self.ip[ip].append(now)


# ---------------------------------------------------------------- startup

app = FastAPI(title="Temporal Consistency")
LIVE = make_live_backend()
LIVE_LOCK = threading.Lock()          # one live answer at a time (a local model uses the whole CPU)
LIMITER = Limiter()


def load_prepared() -> dict:
    path = DEMO_DIR / "answers.json"
    if not path.exists():
        return {"items": {}, "showcase": []}
    return json.loads(path.read_text(encoding="utf-8"))


def load_index(conn) -> list[dict]:
    """One row per decision for the list view; computed once at startup."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='commit_fts'").fetchone():
        conn.executescript(FTS_SCHEMA)
    fixed = {r[0] for r in conn.execute("SELECT fixed_commit FROM fix_links WHERE fixed_commit IS NOT NULL")}
    has_records = conn.execute("SELECT 1 FROM sqlite_master WHERE name='decision_records'").fetchone()
    records = {r[0] for r in conn.execute("SELECT DISTINCT commit_hash FROM decision_records")} if has_records else set()
    out = []
    for h, subject, author, committer, authored, applied, decider in conn.execute("""
            SELECT c.commit_hash, c.subject, c.author_name, c.committer_name, ct.authored_utc, ct.applied_utc,
                   a.actor_id FROM commits c JOIN commit_times ct USING (commit_hash)
            LEFT JOIN actors a ON a.email = lower(c.committer_email) ORDER BY ct.applied_utc DESC"""):
        out.append(dict(commit=h, subject=subject, author=author, decider=committer, applied=iso(applied),
                        applied_utc=applied, later_fixed=h in fixed, has_record=h in records,
                        known=len(info_set(conn, decider, applied, "knew", exclude=h))))
    return out


PREPARED = load_prepared()
_c = db()
INDEX = load_index(_c)
_c.close()
PREPARED_COMMITS = {s["commit"]: s for s in PREPARED.get("showcase", [])}
for row in INDEX:
    row["prepared"] = row["commit"] in PREPARED_COMMITS
RESULTS_CACHE: dict = {}


# ---------------------------------------------------------------- helpers

def full_message(conn, h: str) -> str:
    subject, body = conn.execute("SELECT subject, body FROM commits WHERE commit_hash=?", (h,)).fetchone()
    return f"{subject}\n\n{(body or '').strip()}"


def find_decision(conn, commit: str) -> dict:
    if not re.fullmatch(r"[0-9a-fA-F]{6,40}", commit or ""):
        raise HTTPException(400, "A commit is a hash of 6 to 40 hex characters.")
    try:
        return decision(conn, commit.lower())
    except SystemExit as e:                       # ledger.decision reports no or ambiguous match this way
        raise HTTPException(404, str(e))


def latest_record(conn, h: str) -> dict | None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='decision_records'").fetchone():
        return None
    row = conn.execute("""SELECT model, prompt_version, record_json FROM decision_records WHERE commit_hash=?
        ORDER BY prompt_version DESC, created_at DESC LIMIT 1""", (h,)).fetchone()
    if not row:
        return None
    rec = json.loads(row[2])
    return dict(model=row[0], prompt_version=row[1], kind=rec.get("kind"),
                rationale_stated=rec.get("rationale_stated"), fields={k: rec.get(k, []) for k in FIELDS})


def enrich(conn, r: dict, anon: Anonymizer, prepared: bool) -> dict:
    """Add the text of every evidence item the model saw, with real names and as the model saw it."""
    named = Session(conn, r["commit"], r["policy"], NoAnon(), named=True)
    hidden = Session(conn, r["commit"], r["policy"], anon)
    d = named.d
    evidence = {}
    for i, h in r.get("evidence_ids", {}).items():
        if i == "D0":
            text = full_message(conn, h)
            evidence[i] = dict(kind="message", commit=h, label="The patch's own message",
                               text=text, text_anon=anon.hide(text))
        elif i == "M0":
            evidence[i] = dict(kind="facts", commit=h, label="Facts recorded by the system",
                               text=named.shown["M0"], text_anon=hidden.shown["M0"])
        else:
            applied = conn.execute("SELECT applied_utc FROM commit_times WHERE commit_hash=?", (h,)).fetchone()[0]
            subject = conn.execute("SELECT subject FROM commits WHERE commit_hash=?", (h,)).fetchone()[0]
            text = full_message(conn, h)
            evidence[i] = dict(kind="earlier", commit=h, label=subject, applied=iso(applied),
                               future=seen_time(conn, h, d["decider"]) > d["applied"],
                               text=text, text_anon=anon.hide(text))
    out = dict(r)
    out["evidence"] = evidence
    out["future_shown"] = sum(1 for e in evidence.values() if e.get("future"))
    out["anon"] = dict(summary=anon.hide(r.get("summary", "")),
                       claims=[dict(claim=anon.hide(c["claim"]), quote=anon.hide(c["quote"])) for c in r["claims"]])
    out["prepared"] = prepared
    return out


def answer_one(conn, commit: str, question: str, policy: str, loop: str, ip: str) -> dict:
    if policy not in POLICIES:
        raise HTTPException(400, f"Unknown policy {policy!r}.")
    d = find_decision(conn, commit)
    anon = Anonymizer(conn)
    k = demo_key(d["hash"], policy, loop, question)
    if k in PREPARED["items"]:
        return enrich(conn, PREPARED["items"][k], anon, prepared=True)
    if LIVE is None:
        raise HTTPException(503, "Live answers are off on this server. Choose one of the prepared questions.")
    LIMITER.take(ip)
    with LIVE_LOCK:
        try:
            r = answer(conn, d["hash"], question, LIVE, policy, loop, anon=anon)
        except ConnectionError as e:
            raise HTTPException(503, f"The model is not reachable: {e}")
    return enrich(conn, r, anon, prepared=False)


# ---------------------------------------------------------------- API

class AskBody(BaseModel):
    commit: str
    question: str
    policy: str = "B3"
    loop: str = "prefetch"


@app.get("/api/status")
def status():
    return dict(decisions=len(INDEX), prepared_decisions=len(PREPARED_COMMITS),
                prepared_answers=len(PREPARED["items"]), live=LIVE is not None,
                live_model=getattr(LIVE, "name", None), policies=POLICIES)


@app.get("/api/decisions")
def decisions():
    return INDEX


@app.get("/api/decision/{commit}")
def decision_detail(commit: str):
    conn = db()
    try:
        d = find_decision(conn, commit)
        h, t = d["hash"], d["applied"]
        anon = Anonymizer(conn)
        named = Session(conn, h, "B3", NoAnon(), named=True)
        hidden = Session(conn, h, "B3", anon)
        message = full_message(conn, h)
        knew = info_set(conn, d["decider"], t, "knew", exclude=h)
        could = info_set(conn, d["decider"], t, "could", exclude=h)
        hs = hindsight(conn, h, t)
        fixes = [dict(commit=fh, applied=iso(ap), applied_utc=ap, subject=sj, message=full_message(conn, fh))
                 for fh, ap, sj in hs["fixes"]]
        fix_set = {f["commit"] for f in fixes}
        later = [dict(commit=lh, applied=iso(ap), applied_utc=ap, subject=sj)
                 for lh, ap, sj in hs["later_same_files"] if lh not in fix_set]
        people = [dict(name=name, name_anon=anon.hide(email.lower()) if email else name, roles=roles.split(","))
                  for name, email, roles in d["people"]]
        return dict(
            commit=h, subject=d["subject"], authored=iso(d["authored"]), authored_utc=d["authored"],
            applied=iso(t), applied_utc=t,
            decider=named.who(d["decider"]), decider_anon=hidden.who(d["decider"]), people=people,
            files=files_of(conn, h), message=message, message_anon=anon.hide(message),
            facts=named.shown["M0"], facts_anon=hidden.shown["M0"], record=latest_record(conn, h),
            known=[dict(commit=k[0], subject=k[3], roles=(k[4] or "").split(","), seen=iso(k[5]), seen_utc=k[5])
                   for k in knew],
            public_count=len(could), later_fixes=fixes, later_same_files=later,
            prepared_questions=PREPARED_COMMITS.get(h, {}).get("questions", []))
    finally:
        conn.close()


@app.post("/api/ask")
def ask(body: AskBody, request: Request):
    conn = db()
    try:
        return answer_one(conn, body.commit, body.question, body.policy, body.loop, request.client.host)
    finally:
        conn.close()


@app.post("/api/compare")
def compare(body: AskBody, request: Request):
    """The same question with only the decider's evidence (B3) and with no time filter (B1)."""
    conn = db()
    try:
        return {p: answer_one(conn, body.commit, body.question, p, body.loop, request.client.host)
                for p in ("B3", "B1")}
    finally:
        conn.close()


@app.get("/api/results")
def results():
    path = DEMO_DIR / "results.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if not RESULTS_CACHE:                          # no prepared file: compute once (a few seconds)
        conn = db()
        try:
            RESULTS_CACHE.update(retrieval=retrieval_rows(conn), evaluation=eval_rows(conn), built_at=None)
        finally:
            conn.close()
    return RESULTS_CACHE


if WEB_DIST.exists():                              # the built React app; must be mounted after the API
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")

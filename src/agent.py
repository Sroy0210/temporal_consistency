"""Answer questions about one decision using only what the decider could have known.
 
  python agent.py ask  --commit <hash> --question "Why was this patch applied?"
  python agent.py ask  --commit <hash> --question "..." --loop tools --policy B1
  python agent.py eval --questions eval/questions.jsonl --policy B1,B3 --per-type 10
  python agent.py report
  python agent.py selftest
 
How the time boundary is kept (in code, not in the prompt):
  - A session fixes the decision, the decider and the time t before the model sees anything.
  - The policy turns that into an allowed set of earlier patches (B3 = the decider's information set).
  - Tools search and read only inside the allowed set. They take no dates and no hashes.
  - The model cites evidence IDs (D0, M0, E1 ...). Code keeps a claim only if its quote is
    verbatim in an item the model was actually shown.
  - Hindsight (later fixes, later patches to the same files) is attached by code after the
    answer. The model never sees it.
 
Loops:
  prefetch  code calls the tools up front; one model call answers (reliable on small local models)
  tools     the model calls the tools itself, up to MAX_TOOL_CALLS, then answers
"""
from __future__ import annotations
 
import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import time
import urllib.error
import urllib.request
 
from ledger import decision, files_of, hindsight, info_set, iso
from local_eval import FTS_SCHEMA, ranked, seen_time
from rule_based_checks import Anonymizer, contains_leak, leak_terms, verify_quote
 
POLICIES = {
    "B1": "no time filter",
    "B2a": "cutoff on author date",
    "B2b": "cutoff on applied date (everything public at decision time)",
    "B3": "information set (earlier patches the decider took part in)",
}
MAX_TOOL_CALLS = 4
D0_CHARS, PATCH_CHARS, PREFETCH_CHARS = 4000, 1500, 1000   # keeps everything inside num_ctx 8192
PREFETCH_RELATED, PREFETCH_SEARCH, SEARCH_K = 3, 3, 5
STATUSES = ["answered", "not_in_evidence", "premise_false"]
VERDICTS = ["yes", "no", "unknown", "n/a"]
 
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {"type": "array", "items": {"type": "object", "properties": {
            "claim": {"type": "string"}, "quote": {"type": "string"}, "source": {"type": "string"}},
            "required": ["claim", "quote", "source"]}},
        "summary": {"type": "string"},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": STATUSES},
        "verdict": {"type": "string", "enum": VERDICTS},
    },
    "required": ["claims", "summary", "unknowns", "status", "verdict"],
}
 
INSTRUCTIONS = """You answer one question about one decision in the history of the Linux kernel's ext4 filesystem.
The decision is a patch being applied by the maintainer (the decider). You are shown only evidence the
decider could have had at that moment.
 
Evidence items have IDs:
  D0  the patch's own commit message (its author's reasoning)
  M0  facts recorded by the system: dates, who applied it, who reviewed it
  E1, E2 ...  earlier patches
 
Rules:
- Every claim needs a quote copied word for word from ONE evidence item, and that item's ID as source.
  Use the shortest span that supports the claim (one sentence or less). Never quote the question.
- Use only the evidence. Do not use anything you remember about Linux, ext4 or these people.
  People appear as P001, P002 ...; links as LINK001 .... Do not guess who they are.
- You cannot know anything that happened after the decision. Do not mention later events.
- The reasoning in D0 is the patch author's. Write "the patch message states ...", not "the maintainer believed ...".
- If the question assumes something the evidence contradicts (for example it says the patch was
  rejected, but M0 says it was applied), set status "premise_false" and cite the contradiction.
- If the evidence does not answer the question, set status "not_in_evidence" and say what is missing
  in "unknowns". That is a correct answer, not a failure.
- For yes/no questions set verdict "yes" or "no" with a supporting claim, or "unknown". Otherwise "n/a".
- "summary": at most 3 sentences, restating only your claims.
"""
 
TOOLS_NOTE = f"""
Tools: search_context and related_prior list earlier patches by ID; read_patch shows one in full.
You can cite an earlier patch only after reading it with read_patch. You may make at most {MAX_TOOL_CALLS} calls.
Your FIRST reply must be a tool call (start with related_prior). Answer only after you have looked.
"""
 
TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "search_context", "description": "Keyword search over earlier patches you are allowed to see.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "related_prior", "description": "Earlier patches that changed the same files, newest first.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "read_patch", "description": "Full message of one earlier patch, by an ID such as E3.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}},
]
 
RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
 run_id INTEGER PRIMARY KEY,
 qkey TEXT, qtype TEXT, expected TEXT,          -- set by eval; NULL for ad-hoc ask
 commit_hash TEXT, question TEXT, policy TEXT, loop TEXT, model TEXT, named INTEGER,
 status TEXT, verdict TEXT, claims INTEGER, rejected INTEGER,
 leaks_raw INTEGER, leaks_final INTEGER, future_cites INTEGER, refused INTEGER, tool_calls INTEGER,
 prompt_tokens INTEGER, output_tokens INTEGER, seconds REAL,
 result_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_runs_q ON agent_runs(qkey, policy, loop, model, named);
"""
# ---------------------------------------------------------------- Tools call helpers

_TEXT_CALL = re.compile(r'"name"\s*:\s*"(search_context|related_prior|read_patch)"'
                        r'|\b(search_context|related_prior|read_patch)\s*\(([^)]*)\)')


def text_tool_calls(content: str) -> list[dict]:
    """Small models sometimes write a tool call as text instead of using the tool-call field."""
    calls = []
    for m in _TEXT_CALL.finditer(content or ""):
        name = m.group(1) or m.group(2)
        tail = content[m.end(): m.end() + 200] if m.group(1) else (m.group(3) or "")
        arg = re.search(r'["\']?(query|id)["\']?\s*[:=]\s*["\']([^"\']+)', tail)
        bare = re.search(r'["\']([^"\']+)["\']', tail) if not arg else None
        args = {arg.group(1): arg.group(2)} if arg else (
            {"query" if name == "search_context" else "id": bare.group(1)} if bare and name != "related_prior" else {})
        calls.append({"function": {"name": name, "arguments": args}})
    return calls
 
 
class NoAnon:
    """Used with --named: real names go to the model (to measure memorization)."""
    def hide(self, text: str) -> str:
        return text
 
    def show(self, text: str) -> str:
        return text
 
 
# ---------------------------------------------------------------- session: the boundary lives here
 
class Session:
    LABELS = {"D0": "the patch's own message", "M0": "facts recorded by the system"}
 
    def __init__(self, conn, commit: str, policy: str = "B3", anon=None, named: bool = False):
        if policy not in POLICIES:
            raise SystemExit(f"unknown policy {policy!r}; choose from {', '.join(POLICIES)}")
        self.conn, self.policy, self.named = conn, policy, named
        self.d = decision(conn, commit)
        self.h, self.t, self.actor = self.d["hash"], self.d["applied"], self.d["decider"]
        self.anon = anon or (NoAnon() if named else Anonymizer(conn))
        self.allowed = self._allowed()                     # hash -> applied_utc
        self.ids: dict[str, str] = {}                      # hash -> E-id
        self.hashes: dict[str, str] = {}                   # E-id -> hash
        self.shown = {"D0": self._message(self.h, D0_CHARS), "M0": self._m0()}   # what the model saw
        self.trace: list[dict] = []
        self.refused = 0
 
    def _allowed(self) -> dict[str, int]:
        c, t, me = self.conn, self.t, self.h
        if self.policy == "B3":
            return {r[0]: r[2] for r in info_set(c, self.actor, t, "knew", exclude=me)}
        if self.policy == "B2b":
            return {r[0]: r[2] for r in info_set(c, self.actor, t, "could", exclude=me)}
        cond = "authored_utc <= ?" if self.policy == "B2a" else "? IS NOT NULL"
        return dict(c.execute(f"SELECT commit_hash, applied_utc FROM commit_times "
                              f"WHERE commit_hash != ? AND {cond}", (me, t)))
 
    def who(self, aid: int | None) -> str:
        if aid is None:
            return "unknown"
        if self.named:
            row = self.conn.execute("SELECT name, email FROM actors WHERE actor_id=?", (aid,)).fetchone()
            return (row[0] or row[1]) if row else "unknown"
        return f"P{aid:03d}"
 
    def _message(self, h: str, cap: int) -> str:
        subject, body = self.conn.execute("SELECT subject, body FROM commits WHERE commit_hash=?", (h,)).fetchone()
        text = self.anon.hide(f"{subject}\n\n{(body or '').strip()}")
        return text if len(text) <= cap else text[:cap] + "\n[... message cut]"
 
    def _m0(self) -> str:
        roles: dict[str, list[str]] = {}
        for aid, role in self.conn.execute("SELECT actor_id, role FROM participation WHERE commit_hash=?", (self.h,)):
            roles.setdefault(role, []).append(self.who(aid))
        fmt = lambda r: ", ".join(sorted(set(roles.get(r, [])))) or "none"
        n = len(self.allowed)
        return "\n".join([
            f"Patch: {self.anon.hide(self.d['subject'])}",
            f"Status: applied on {iso(self.t)} by {self.who(self.actor)} (the decider).",
            f"Written by: {fmt('author')}, on {iso(self.d['authored'])}.",
            f"Reviewed-by: {fmt('reviewed_by')}.",
            f"Acked-by: {fmt('acked_by')}.",
            f"Tested-by: {fmt('tested_by')}.",
            f"Reported-by: {fmt('reported_by')}.",
            f"Files changed: {', '.join(files_of(self.conn, self.h)) or 'none in scope'}.",
            f"Earlier patches available as evidence: {n}." if n else
            "Earlier patches available as evidence: none. The decider took part in no earlier patch in this history."
            if self.policy == "B3" else "Earlier patches available as evidence: none.",
        ])
 
    # --- evidence IDs ---------------------------------------------------------------------------
 
    def _eid(self, h: str) -> str:
        if h not in self.ids:
            eid = f"E{len(self.ids) + 1}"
            self.ids[h], self.hashes[eid] = eid, h
        return self.ids[h]
 
    def _line(self, h: str) -> str:
        subject = self.conn.execute("SELECT subject FROM commits WHERE commit_hash=?", (h,)).fetchone()[0]
        roles = self.conn.execute("SELECT GROUP_CONCAT(role) FROM participation WHERE commit_hash=? AND actor_id=?",
                                  (h, self.actor)).fetchone()[0]
        return (f"{self._eid(h)}  applied {iso(self.allowed[h])[:10]}  {self.anon.hide(subject)}"
                f"  [decider: {roles or 'not involved'}]")
 
    def _search_hashes(self, query: str, k: int) -> list[str]:
        if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='commit_fts'").fetchone():
            self.conn.executescript(FTS_SCHEMA)
        return [h for h in ranked(self.conn, query) if h in self.allowed][:k]
 
    def _related_hashes(self, k: int) -> list[str]:
        files = files_of(self.conn, self.h)
        if not files:
            return []
        rows = self.conn.execute(f"SELECT DISTINCT commit_hash FROM file_changes WHERE in_scope=1 "
                                 f"AND new_path IN ({','.join('?' * len(files))})", files).fetchall()
        return sorted((h for (h,) in rows if h in self.allowed), key=lambda h: self.allowed[h], reverse=True)[:k]
 
    # --- tools (all bounded to the allowed set) ------------------------------------------------
 
    def search_context(self, query: str) -> str:
        return "\n".join(self._line(h) for h in self._search_hashes(query, SEARCH_K)) or "No matching earlier patches."
 
    def related_prior(self) -> str:
        return "\n".join(self._line(h) for h in self._related_hashes(SEARCH_K)) or "No earlier patches touched these files."
 
    def read_patch(self, eid: str, cap: int = PATCH_CHARS) -> str:
        eid = str(eid).strip().upper()
        h = self.hashes.get(eid)
        if h is None:            # never listed to the model, so it cannot be inside the allowed set
            self.refused += 1
            return f"Refused: {eid!r} is not an ID you have been shown. Use search_context or related_prior first."
        self.shown[eid] = self._message(h, cap)
        return f"{eid}  applied {iso(self.allowed[h])}\n{self.shown[eid]}"
 
    def call(self, name: str, args: dict) -> str:
        args = args if isinstance(args, dict) else {}
        if name == "search_context":
            out = self.search_context(str(args.get("query", "")))
        elif name == "related_prior":
            out = self.related_prior()
        elif name == "read_patch":
            out = self.read_patch(args.get("id", ""))
        else:
            self.refused += 1
            out = f"Unknown tool {name!r}."
        self.trace.append({"tool": name, "args": args, "result": out[:300]})
        return out
 
    def prefetch(self, question: str) -> None:
        """Prefetch loop: code runs related_prior + search and reads the results for the model."""
        hs = self._related_hashes(PREFETCH_RELATED)
        for h in self._search_hashes(self.d["subject"] + " " + question, PREFETCH_SEARCH + len(hs)):
            if h not in hs and len(hs) < PREFETCH_RELATED + PREFETCH_SEARCH:
                hs.append(h)
        for h in hs:
            self.read_patch(self._eid(h), PREFETCH_CHARS)
        self.trace.append({"tool": "prefetch", "args": {}, "result": ", ".join(self.ids[h] for h in hs) or "none"})
 
    def evidence(self) -> str:
        blocks = []
        for i, text in self.shown.items():
            label = self.LABELS.get(i) or f"earlier patch, applied {iso(self.allowed[self.hashes[i]])}"
            blocks.append(f"=== {i}: {label} ===\n{text}")
        return "\n\n".join(blocks)
 
 
# ---------------------------------------------------------------- backends
 
class OllamaAgent:
    def __init__(self, model: str, host: str):
        self.name, self.url = model, host.rstrip("/") + "/api/chat"
        self.think_ok = True
 
    def _post(self, body: dict) -> dict:
        if self.think_ok:
            body["think"] = False
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if "think" in detail.lower() and self.think_ok:
                self.think_ok = False
                body.pop("think", None)
                return self._post(body)
            raise RuntimeError(f"Ollama HTTP {e.code}: {detail[:300]}") from None
        except urllib.error.URLError as e:
            raise ConnectionError(f"Cannot reach Ollama at {self.url}. Is it running? ({e.reason})") from None
 
    @staticmethod
    def _usage(data: dict) -> dict:
        return {"prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")}
 
    def _body(self, messages, num_predict, **extra) -> dict:
        return {"model": self.name, "stream": False, "keep_alive": "30m", "messages": messages,
                "options": {"temperature": 0, "num_ctx": 8192, "num_predict": num_predict}, **extra}
 
    def step(self, messages: list[dict], tools: list[dict]) -> tuple[dict, dict]:
        data = self._post(self._body(messages, 300, tools=tools))
        return data["message"], self._usage(data)
 
    def answer(self, messages: list[dict], schema: dict) -> tuple[str, dict]:
        data = self._post(self._body(messages, 1200, format=schema))
        return data["message"]["content"], self._usage(data)
 
 
class GeminiAgent:
    """Prefetch loop only for now; the tools loop on Gemini comes once a key is available to test it."""
 
    def __init__(self, model: str):
        try:
            from google import genai
        except ImportError:
            raise SystemExit("pip install google-genai  (needed for --backend gemini)")
        self.client, self.name = genai.Client(), model
 
    def step(self, messages, tools):
        raise SystemExit("--loop tools is not wired for Gemini yet; use --loop prefetch")
 
    def answer(self, messages: list[dict], schema: dict) -> tuple[str, dict]:
        rest = "\n\n".join(f"[{m['role']}]\n{m.get('content', '')}" for m in messages[1:])
        r = self.client.models.generate_content(
            model=self.name, contents=rest,
            config={"system_instruction": messages[0]["content"], "temperature": 0,
                    "response_mime_type": "application/json", "response_schema": schema})
        u = getattr(r, "usage_metadata", None)
        return r.text, {"prompt_tokens": getattr(u, "prompt_token_count", None),
                        "output_tokens": getattr(u, "candidates_token_count", None)}
 
 
class FakeAgent:
    """No model. Tools: related_prior, read E1, read E999 (must be refused). Answer: one real quote
    from D0, one invented quote, one citation of an item never shown (both must be rejected)."""
    name = "fake"
 
    def __init__(self):
        self.steps = 0
 
    def step(self, messages, tools):
        self.steps += 1
        plan = {1: ("related_prior", {}), 2: ("read_patch", {"id": "E1"}), 3: ("read_patch", {"id": "E999"})}
        if self.steps in plan:
            n, a = plan[self.steps]
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": n, "arguments": a}}]}, {}
        return {"role": "assistant", "content": "done"}, {}
 
    def answer(self, messages, schema):
        d0 = messages[1]["content"].split("=== D0", 1)[1].split("===\n", 1)[1]
        subject = d0.split("\n", 1)[0]
        return json.dumps({"claims": [
            {"claim": "the patch subject", "quote": subject, "source": "D0"},
            {"claim": "invented", "quote": "this sentence was never written by anyone", "source": "D0"},
            {"claim": "unseen item", "quote": subject, "source": "E99"}],
            "summary": "Test answer.", "unknowns": [], "status": "answered", "verdict": "n/a"}), {}
 
 
# ---------------------------------------------------------------- answer + checks
 
def parse_answer(raw: str) -> dict:
    data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    claims = [c for c in data.get("claims") or [] if isinstance(c, dict) and c.get("quote") and c.get("source")]
    for c in claims:
        c["claim"] = str(c.get("claim") or "")
    return dict(claims=claims, summary=str(data.get("summary") or ""),
                unknowns=[str(u) for u in data.get("unknowns") or []],
                status=data.get("status") if data.get("status") in STATUSES else "answered",
                verdict=data.get("verdict") if data.get("verdict") in VERDICTS else "n/a")
 
 
def validate(s: Session, data: dict) -> tuple[list[dict], list[str]]:
    kept, failed = [], []
    for c in data["claims"]:
        src = str(c["source"]).strip().upper()
        text = s.shown.get(src)
        if text is None:
            failed.append(f"[{src}] {c['quote'][:150]}  (this ID was not shown to you)")
        elif not verify_quote(c["quote"], text):
            failed.append(f"[{src}] {c['quote'][:150]}")
        else:
            kept.append({**c, "source": src})
    return kept, failed
 
 
def answer(conn, commit: str, question: str, backend, policy: str = "B3", loop: str = "prefetch",
           named: bool = False, anon: Anonymizer | None = None, retries: int = 1) -> dict:
    t0 = time.time()
    real = anon or Anonymizer(conn)
    s = Session(conn, commit, policy, NoAnon() if named else real, named)
    q = real.show(question) if named else real.hide(question)
    usage = {"prompt_tokens": 0, "output_tokens": 0}
 
    def add(u):
        for k in usage:
            usage[k] += (u or {}).get(k) or 0
 
    if loop == "prefetch":
        s.prefetch(q)
    system = INSTRUCTIONS + (TOOLS_NOTE if loop == "tools" else "")
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": f"{s.evidence()}\n\nQUESTION: {q}"}]
    raw_all: list[str] = []
 
    if loop == "tools":
        calls_made = 0
        while calls_made < MAX_TOOL_CALLS:
            msg, u = backend.step(messages, TOOL_SPECS)
            add(u)
            calls = (msg.get("tool_calls") or text_tool_calls(msg.get("content")))[: MAX_TOOL_CALLS - calls_made]
            if not calls and msg.get("content"):        # record what it said instead of calling a tool
                s.trace.append({"tool": "note", "args": {}, "result": msg["content"][:300]})
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             **({"tool_calls": calls} if calls else {})})
            if msg.get("content"):
                raw_all.append(msg["content"])
            if not calls:
                break
            for c in calls:
                f = c.get("function") or {}
                args = f.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                messages.append({"role": "tool", "tool_name": f.get("name", ""),
                                 "content": s.call(f.get("name", ""), args)})
                calls_made += 1
    messages.append({"role": "user", "content": "Now give your final answer as JSON. You may cite only these IDs: "
                     + ", ".join(s.shown) + "."})
 
    attempt, failed, data, kept = 0, [], None, []
    while True:
        attempt += 1
        raw, u = backend.answer(messages, ANSWER_SCHEMA)
        add(u)
        raw_all.append(raw)
        try:
            data = parse_answer(raw)
            kept, failed = validate(s, data)
        except (ValueError, json.JSONDecodeError) as e:
            data, kept, failed = None, [], [f"(reply was not valid JSON: {e})"]
        if not failed or attempt > retries:
            break
        messages += [{"role": "assistant", "content": raw},
                     {"role": "user", "content": "These claims were rejected: the quote is not word for word in the "
                      "cited item, or the item was not shown to you:\n" + "\n".join(f"- {f}" for f in failed) +
                      "\nAnswer again. Copy quotes exactly from the cited item, or leave the claim out."}]
    if data is None:
        data = dict(claims=[], summary="", unknowns=[], status="not_in_evidence", verdict="unknown")
 
    status, verdict = data["status"], data["verdict"]
    if not kept:                                  # no surviving evidence: no answer, whatever the model said
        status = "not_in_evidence"
        verdict = "unknown" if verdict in ("yes", "no") else verdict
    elif status == "not_in_evidence":
        status = "answered"
 
    terms = leak_terms(conn, s.h, s.t)
    final_text = " ".join([data["summary"], *(c["claim"] + " " + c["quote"] for c in kept)])
    claims = []
    for c in kept:
        src_hash = s.h if c["source"] in ("D0", "M0") else s.hashes[c["source"]]
        future = c["source"].startswith("E") and seen_time(conn, src_hash, s.actor) > s.t
        claims.append(dict(claim=real.show(c["claim"]), quote=real.show(c["quote"]), source=c["source"],
                           commit=src_hash, future=bool(future)))
    hs = hindsight(conn, s.h, s.t)
    return dict(
        commit=s.h, subject=s.d["subject"], question=question, policy=policy, loop=loop, model=backend.name,
        named=named, decider=real.show(s.who(s.actor)) if not named else s.who(s.actor),
        decided_at=iso(s.t), evidence_available=len(s.allowed),
        status=status, model_status=data["status"], verdict=verdict, summary=real.show(data["summary"]), claims=claims,
        unknowns=[real.show(x) for x in data["unknowns"]], rejected=[real.show(f) for f in failed],
        hindsight=dict(fixes=[dict(commit=h, applied=iso(ap), subject=sj) for h, ap, sj in hs["fixes"]],
                       later_same_files=[dict(commit=h, applied=iso(ap), subject=sj)
                                         for h, ap, sj in hs["later_same_files"]]),
        evidence_ids={i: (s.h if i in ("D0", "M0") else s.hashes[i]) for i in s.shown},
        trace=s.trace, refused=s.refused, tool_calls=sum(1 for x in s.trace if x["tool"] not in ("prefetch", "note")),
        leaks_raw=contains_leak("\n".join(raw_all), terms), leaks_final=contains_leak(final_text, terms),
        future_cites=sum(c["future"] for c in claims), attempts=attempt,
        seconds=round(time.time() - t0, 1), **usage)
 
 
def store(conn, r: dict, qkey=None, qtype=None, expected=None) -> None:
    conn.execute("""INSERT INTO agent_runs(qkey, qtype, expected, commit_hash, question, policy, loop, model, named,
        status, verdict, claims, rejected, leaks_raw, leaks_final, future_cites, refused, tool_calls,
        prompt_tokens, output_tokens, seconds, result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (qkey, qtype, expected, r["commit"], r["question"], r["policy"], r["loop"], r["model"], int(r["named"]),
         r["status"], r["verdict"], len(r["claims"]), len(r["rejected"]), len(r["leaks_raw"]), len(r["leaks_final"]),
         r["future_cites"], r["refused"], r["tool_calls"], r["prompt_tokens"], r["output_tokens"], r["seconds"],
         json.dumps(r)))
    conn.commit()
 
 
def show(r: dict) -> None:
    print("=" * 90)
    print(f"DECISION  {r['commit'][:12]}  {r['subject']}")
    print(f"  applied {r['decided_at']} by {r['decider']} (decider) | policy {r['policy']} | "
          f"{r['evidence_available']} earlier patches allowed | {r['loop']} | {r['model']}")
    print(f"QUESTION  {r['question']}")
    print(f"STATUS    {r['status']}   VERDICT {r['verdict']}")
    print("-" * 90 + "\nAT THE TIME")
    for c in r["claims"]:
        print(f"  - {c['claim']}{'   [FUTURE EVIDENCE]' if c['future'] else ''}\n      \"{c['quote']}\"  [{c['source']}]")
    if r["summary"]:
        print(f"SUMMARY   {r['summary']}")
    for u in r["unknowns"]:
        print(f"UNKNOWN   {u}")
    for f in r["rejected"]:
        print(f"REJECTED  {f}")
    print("-" * 90 + "\nWITH HINDSIGHT (not available to the decider; attached by code)")
    fix_hashes = {x["commit"] for x in r["hindsight"]["fixes"]}
    for x in r["hindsight"]["fixes"]:
        print(f"  later fix  {x['applied'][:10]}  {x['commit'][:12]}  {x['subject']}")
    for x in [x for x in r["hindsight"]["later_same_files"] if x["commit"] not in fix_hashes][:3]:
        print(f"  same files {x['applied'][:10]}  {x['commit'][:12]}  {x['subject']}")
    if not (r["hindsight"]["fixes"] or r["hindsight"]["later_same_files"]):
        print("  nothing later in this history")
    print("-" * 90)
    print(f"CHECKS    claims kept {len(r['claims'])}, rejected {len(r['rejected'])} | future citations "
          f"{r['future_cites']} | leak terms in raw output {len(r['leaks_raw'])} | refused tool calls "
          f"{r['refused']} | tries {r['attempts']} | {r['seconds']}s")
    calls = [x for x in r["trace"] if x["tool"] != "note"]
    print("TRACE     " + (" | ".join(f"{x['tool']}({json.dumps(x['args']) if x['args'] else ''})" for x in calls)
                          or "no tool calls (answered from D0 and M0 only)"))
    for x in r["trace"]:
        if x["tool"] == "note":
            print(f"MODEL SAID instead of calling a tool: {x['result']!r}")
 
 
# ---------------------------------------------------------------- eval + report
 
def qkey(q: dict) -> str:
    return hashlib.sha1((q["commit"] + "|" + q["question"]).encode()).hexdigest()[:16]
 
 
def evaluate(conn, args, backend) -> None:
    with open(args.questions, encoding="utf-8") as f:
        qs = [json.loads(line) for line in f if line.strip()]
    rnd, chosen = random.Random(7), []
    for ty in args.types.split(","):
        pool = [q for q in qs if q["type"] == ty]
        rnd.shuffle(pool)
        chosen += pool[: args.per_type]
    policies = args.policy.split(",")
    anon = Anonymizer(conn)
    total, i = len(chosen) * len(policies), 0
    print(f"{len(chosen)} questions x {len(policies)} policies = {total} runs ({args.loop}, {backend.name})", flush=True)
    for q in chosen:
        for pol in policies:
            i += 1
            k = qkey(q)
            if conn.execute("SELECT 1 FROM agent_runs WHERE qkey=? AND policy=? AND loop=? AND model=? AND named=?",
                            (k, pol, args.loop, backend.name, int(args.named))).fetchone():
                continue                                       # done in an earlier run
            try:
                r = answer(conn, q["commit"], q["question"], backend, pol, args.loop, args.named, anon, args.retries)
            except ConnectionError as e:
                raise SystemExit(f"ERROR: {e}")
            except Exception as e:                             # one bad run must not stop the evaluation
                print(f"  [{i}/{total}] {q['type']:12} {pol:4} {q['commit'][:12]} FAILED {type(e).__name__}: {e}", flush=True)
                continue
            store(conn, r, k, q["type"], q.get("answer"))
            print(f"  [{i}/{total}] {q['type']:12} {pol:4} {q['commit'][:12]} {r['status']:15} {r['verdict']:7} "
                  f"claims {len(r['claims'])}/{len(r['claims']) + len(r['rejected'])}  leaks {len(r['leaks_raw'])}  "
                  f"future {r['future_cites']}  {r['seconds']}s", flush=True)
    report(conn, args)
 
 
def report(conn, args) -> None:
    rows = conn.execute("""SELECT policy, loop, model, named, qtype, expected, status, verdict, claims, rejected,
        leaks_raw, future_cites, seconds FROM agent_runs WHERE qtype IS NOT NULL ORDER BY model, loop, policy""").fetchall()
    if not rows:
        print("no evaluation runs yet")
        return
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault(r[:4], []).append(r)
    pct = lambda a, b: f"{100 * a / b:.0f}% ({a}/{b})" if b else "-"
    print(f"\n{'policy':6} {'loop':9} {'model':16} {'runs':>5}  {'quotes valid':>14}  {'future leak':>13}  "
          f"{'presence right':>15}  {'fake premise caught':>19}  {'no evidence':>12}  {'avg s':>6}")
    for (pol, loop, model, named), rs in groups.items():
        kept, rej = sum(r[8] for r in rs), sum(r[9] for r in rs)
        fl = [r for r in rs if r[4] == "future_leak"]
        pr = [r for r in rs if r[4] == "presence"]
        fp = [r for r in rs if r[4] == "fake_premise"]
        print(f"{pol:6} {loop:9} {(model + (' named' if named else ''))[:16]:16} {len(rs):>5}  "
              f"{pct(kept, kept + rej):>14}  {pct(sum(r[10] > 0 or r[11] > 0 for r in fl), len(fl)):>13}  "
              f"{pct(sum(r[7] == r[5] for r in pr), len(pr)):>15}  "
              f"{pct(sum(r[6] == 'premise_false' for r in fp), len(fp)):>19}  "
              f"{pct(sum(r[6] == 'not_in_evidence' for r in rs), len(rs)):>12}  {sum(r[12] for r in rs) / len(rs):6.1f}")
    print("\nfuture leak = a later fix's hash or subject in the raw output, or a claim citing a patch the decider "
          "could not yet have seen")
 
 
# ---------------------------------------------------------------- self-test (no model)
 
def selftest(conn) -> None:
    anon, failures = Anonymizer(conn), 0
 
    def expect(cond, msg):
        nonlocal failures
        failures += not cond
        print(("PASS  " if cond else "FAIL  ") + msg)
 
    decisions = [r[0] for r in conn.execute("SELECT commit_hash FROM commit_times ORDER BY applied_utc")]
    worst = 0
    for h in decisions:
        s = Session(conn, h, "B3", anon)
        worst += sum(seen_time(conn, x, s.actor) > s.t for x in s.allowed)
    expect(worst == 0, f"B3 allowed sets contain no patch seen after the decision ({len(decisions)} decisions)")
 
    row = conn.execute("SELECT fixed_commit, fixing_commit FROM fix_links WHERE fixed_commit IS NOT NULL LIMIT 1").fetchone()
    target = row[0] if row else decisions[len(decisions) // 2]
    s = Session(conn, target, "B3", anon)
    expect(s.read_patch("E999").startswith("Refused"), "read_patch refuses an ID never listed")
    if row:
        expect(s.read_patch(row[1]).startswith("Refused"), "read_patch refuses a raw hash (the later fix)")
        expect(row[1] not in s.allowed, "B3: the later fix is outside the allowed set")
        expect(row[1] in Session(conn, target, "B1", anon).allowed, "B1: the later fix IS reachable (why B1 leaks)")
    else:
        print("SKIP  no fix-linked decision in this database")
 
    r = answer(conn, target, "Why was this patch applied?", FakeAgent(), "B3", "prefetch", anon=anon)
    expect(len(r["claims"]) == 1 and len(r["rejected"]) == 2,
           f"prefetch: real quote kept, invented quote and unseen ID rejected (kept {len(r['claims'])}, rejected {len(r['rejected'])})")
    expect(r["future_cites"] == 0 and not r["leaks_raw"], "prefetch: no future citations or leak terms")
    expect(bool(r["hindsight"]["fixes"]) == bool(row), "hindsight section attached by code")
 
    r = answer(conn, target, "Why was this patch applied?", FakeAgent(), "B3", "tools", anon=anon)
    expect(r["tool_calls"] == 3 and r["refused"] >= 1,
           f"tools: 3 calls made, the made-up ID refused (calls {r['tool_calls']}, refused {r['refused']})")
    print(f"\n{'all checks passed' if not failures else f'{failures} check(s) FAILED'}")
 
 
# ---------------------------------------------------------------- CLI
 
def make_backend(args):
    if args.backend == "fake":
        return FakeAgent()
    if args.backend == "gemini":
        if not args.model or args.model.startswith("gemma"):
            raise SystemExit("--backend gemini needs --model <a Gemini model id from AI Studio>")
        return GeminiAgent(args.model)
    return OllamaAgent(args.model or "gemma4:e4b", args.host)
 
 
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["ask", "eval", "report", "selftest"])
    p.add_argument("--db", default="data/db/linux.db")
    p.add_argument("--commit", help="hash or prefix (ask)")
    p.add_argument("--question", default="Why was this patch applied?")
    p.add_argument("--policy", default="B3", help="ask: one of B1,B2a,B2b,B3; eval: comma list, e.g. B1,B3")
    p.add_argument("--loop", choices=["prefetch", "tools"], default="prefetch")
    p.add_argument("--backend", choices=["ollama", "gemini", "fake"], default="ollama")
    p.add_argument("--model", help="default gemma4:e4b for ollama")
    p.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    p.add_argument("--named", action="store_true", help="send real names instead of P001... (memorization test)")
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--json", action="store_true", help="ask: print the full result as JSON")
    p.add_argument("--no-store", action="store_true", help="ask: do not save the run")
    p.add_argument("--questions", default="eval/questions.jsonl")
    p.add_argument("--types", default="future_leak,presence,fake_premise")
    p.add_argument("--per-type", type=int, default=10)
    a = p.parse_args()
 
    conn = sqlite3.connect(a.db, timeout=30)
    conn.executescript(RUNS_SCHEMA)
    if a.action == "selftest":
        return selftest(conn)
    if a.action == "report":
        return report(conn, a)
    backend = make_backend(a)
    if a.action == "eval":
        return evaluate(conn, a, backend)
    if not a.commit:
        raise SystemExit("--commit is required for ask")
    try:
        r = answer(conn, a.commit, a.question, backend, a.policy, a.loop, a.named, retries=a.retries)
    except ConnectionError as e:
        raise SystemExit(f"ERROR: {e}")
    if not a.no_store:
        store(conn, r)
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        show(r)
 
 
if __name__ == "__main__":
    main()
 
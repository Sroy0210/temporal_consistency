from __future__ import annotations
 
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
 
from rule_based_checks import Anonymizer, verify_quote
 
PROMPT_VERSION = "v2"
MAX_MESSAGE_CHARS = 8000          # keeps prompt + answer inside a 4096-token context
# Order matters: it is the schema order the model writes in, and the priority for duplicate quotes.
LISTS = ("problem", "change", "rationale", "context", "alternatives", "concerns")
# When two fields have the same quote, the one earlier in this order is kept and the later one dropped.
DEDUPE_ORDER = ("rationale", "problem", "change", "context", "alternatives", "concerns")
KINDS = ["bugfix", "feature", "refactor", "cleanup", "performance", "test", "docs", "other"]
 
ITEM = {"type": "object", "properties": {"point": {"type": "string"}, "quote": {"type": "string"}},
        "required": ["point", "quote"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        **{k: {"type": "array", "items": ITEM} for k in LISTS},
        "rationale_stated": {"type": "boolean"},
    },
    "required": ["kind", *LISTS, "rationale_stated"],
}
 
INSTRUCTIONS = """You extract the reasoning behind one Linux kernel (ext4) patch from its commit message.
People appear as P001, P002...; links as LINK001; addresses as EMAIL001. Keep those tokens as they are.
 
Return JSON only, matching the schema. Fields:
- kind: one of """ + ", ".join(KINDS) + """
- problem: what was wrong or missing before this patch.
- change: what the patch does, including mechanisms it adds (checks, retries, new structures).
- rationale: why this approach is chosen, or why it is correct or safe.
- context: background the reader needs: how things work today, why something was harmless
  until now, or that this patch prepares for a later one.
- alternatives: ONLY other approaches the message explicitly considers or rejects
  (signals: "instead of", "another option", "we could", "an earlier version", "rather than").
- concerns: ONLY risks, limitations or problems the message says remain OPEN after this patch
  (signals: "still", "remains", "not yet", "known issue", "TODO", "may break").
- rationale_stated: true only if the message explains WHY, not just what.
 
Common mistakes to avoid:
- Background or current behaviour is context, never an alternative.
- "This is needed by the following patch" is context, never a concern.
- A safety mechanism the patch adds ("if X changes, return -ESTALE and retry") is change or
  rationale, never a concern: it is how the patch handles a risk, not an open risk.
 
Each list item is {"point": your one-sentence summary, "quote": exact text from the message}.
Rules:
1. The quote must be copied character for character from the message. Never paraphrase it.
2. Use the SHORTEST exact span that supports the point. Never use the same quote twice.
3. The point must be true of the quote alone; do not add facts from elsewhere.
4. Use only the message. If something is not stated, leave that list empty. Empty is correct;
   guessing is wrong. Most messages have no alternatives and no concerns."""
 
RECORD_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_records (
 commit_hash TEXT NOT NULL REFERENCES commits(commit_hash) ON DELETE CASCADE,
 model TEXT NOT NULL,
 prompt_version TEXT NOT NULL,
 record_json TEXT NOT NULL,        -- names restored; quotes verified against the message
 kept INTEGER NOT NULL,            -- quoted items that passed the check
 rejected INTEGER NOT NULL,        -- items dropped (quote not found) after the last attempt
 attempts INTEGER NOT NULL,
 seconds REAL, prompt_tokens INTEGER, output_tokens INTEGER,
 truncated INTEGER NOT NULL DEFAULT 0,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY (commit_hash, model, prompt_version)
);
"""
 
TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s+\S")
 
 
# ---------------------------------------------------------------- backends
 
class OllamaBackend:
    """Local model through Ollama's HTTP API (http://localhost:11434)."""
 
    def __init__(self, model: str, think: bool, host: str):
        self.model, self.think, self.url = model, think, host.rstrip("/") + "/api/chat"
 
    def generate_json(self, system: str, user: str) -> tuple[str, dict]:
        body = {"model": self.model, "stream": False, "format": SCHEMA, "keep_alive": "30m",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 1200}}
        if not self.think:
            body["think"] = False
        try:
            data = self._post(body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if "think" in detail.lower() and "think" in body:   # model or server without the option
                body.pop("think")
                data = self._post(body)
            else:
                raise RuntimeError(f"Ollama HTTP {e.code}: {detail[:300]}") from None
        usage = {"prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")}
        return data["message"]["content"], usage
 
    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.loads(r.read())
        except urllib.error.URLError as e:
            if isinstance(e, urllib.error.HTTPError):
                raise
            raise RuntimeError(f"Cannot reach Ollama at {self.url}. Is it running? ({e.reason})") from None
 
 
class GeminiBackend:
    """Gemini through AI Studio (env GEMINI_API_KEY) or Vertex AI (env GOOGLE_GENAI_USE_VERTEXAI=true)."""
 
    def __init__(self, model: str):
        try:
            from google import genai
        except ImportError:
            raise SystemExit("pip install google-genai  (needed for --backend gemini)")
        self.client, self.model = genai.Client(), model
 
    def generate_json(self, system: str, user: str) -> tuple[str, dict]:
        r = self.client.models.generate_content(
            model=self.model, contents=user,
            config={"system_instruction": system, "temperature": 0,
                    "response_mime_type": "application/json", "response_schema": SCHEMA})
        u = getattr(r, "usage_metadata", None)
        return r.text, {"prompt_tokens": getattr(u, "prompt_token_count", None),
                        "output_tokens": getattr(u, "candidates_token_count", None)}
 
 
class FakeBackend:
    """No model: quotes the first sentence (and invents one bad quote) to exercise the pipeline."""
 
    def __init__(self):
        self.calls = 0
 
    def generate_json(self, system: str, user: str) -> tuple[str, dict]:
        self.calls += 1
        msg = user.split("MESSAGE:\n", 1)[-1]
        first = re.split(r"(?<=[.:])\s", msg.split("\n\n", 1)[-1].strip(), maxsplit=1)[0][:200]
        bad = [] if "were not found" in user else [{"point": "x", "quote": "this sentence is invented"}]
        out = {"kind": "bugfix", "problem": [{"point": "first sentence", "quote": first}],
               "change": bad, "rationale": [{"point": "dup", "quote": first}], "context": [],
               "alternatives": [], "concerns": [], "rationale_stated": False}
        return json.dumps(out), {"prompt_tokens": len(user) // 4, "output_tokens": 60}
 
 
# ---------------------------------------------------------------- core
 
def message_for(conn, h: str) -> tuple[str, list[str], bool]:
    """Subject + body with the trailing trailer block removed, plus in-scope files."""
    subject, body = conn.execute("SELECT subject, body FROM commits WHERE commit_hash=?", (h,)).fetchone()
    lines = (body or "").rstrip().split("\n")
    while lines and (not lines[-1].strip() or TRAILER_RE.match(lines[-1])):
        lines.pop()
    text = f"{subject}\n\n" + "\n".join(lines).strip()
    truncated = len(text) > MAX_MESSAGE_CHARS
    if truncated:
        text = text[:MAX_MESSAGE_CHARS] + "\n[MESSAGE TRUNCATED]"
    files = [r[0] for r in conn.execute("SELECT DISTINCT new_path FROM file_changes WHERE commit_hash=? "
                                        "AND in_scope=1 AND new_path IS NOT NULL ORDER BY 1", (h,))]
    return text, files, truncated
 
 
def parse(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)         # tolerate a fenced reply
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    data["kind"] = data.get("kind") if data.get("kind") in KINDS else "other"
    for k in LISTS:
        items = data.get(k) or []
        data[k] = [i for i in items if isinstance(i, dict) and i.get("quote") and i.get("point")]
    data["rationale_stated"] = bool(data.get("rationale_stated"))
    return data
 
 
def _key(q: str) -> str:
    return " ".join(q.lower().split()).rstrip(".:;,")
 
 
def check(data: dict, source: str) -> tuple[dict, list[str], int]:
    """Keep items whose quote is verbatim in the source and not already used by an earlier field.
 
    Returns (data, quotes not found, duplicates dropped). Duplicates are removed in code,
    keeping the first field in LISTS order, and do not trigger a retry.
    """
    bad, seen, dupes = [], set(), 0
    for k in DEDUPE_ORDER:
        good = []
        for item in data[k]:
            if not verify_quote(item["quote"], source):
                bad.append(item["quote"])
            elif any(_key(item["quote"]) in s or s in _key(item["quote"]) for s in seen):
                dupes += 1                     
            else:
                seen.add(_key(item["quote"]))
                good.append(item)
        data[k] = good
    return data, bad, dupes
 
 
def extract_one(backend, anon: Anonymizer, conn, h: str, retries: int = 1) -> dict:
    text, files, truncated = message_for(conn, h)
    hidden = anon.hide(text)
    user = (f"Files changed: {', '.join(files) or 'none in scope'}\n\nMESSAGE:\n{hidden}")
    usage_total = {"prompt_tokens": 0, "output_tokens": 0}
    t0, attempt, failed, data, dupes = time.time(), 0, [], None, 0
    while True:
        attempt += 1
        raw, usage = backend.generate_json(INSTRUCTIONS, user)
        for k in usage_total:
            usage_total[k] += usage.get(k) or 0
        try:
            data, failed, dupes = check(parse(raw), hidden)
        except (ValueError, json.JSONDecodeError) as e:
            data, failed = None, [f"(reply was not valid JSON: {e})"]
        if not failed or attempt > retries:
            break
        user = (user + "\n\nYour previous answer had quotes that were not found in the message, "
                "word for word:\n" + "\n".join(f"- {q}" for q in failed) +
                "\nAnswer again. Copy quotes exactly, or leave the item out.")
    if data is None:
        data = {"kind": "other", **{k: [] for k in LISTS}, "rationale_stated": False}
    for k in LISTS:                                            # restore names for storage and display
        for item in data[k]:
            item["point"], item["quote"] = anon.show(item["point"]), anon.show(item["quote"])
    data["duplicates_dropped"] = dupes
    return dict(record=data, kept=sum(len(data[k]) for k in LISTS), rejected=len(failed),
                attempts=attempt, seconds=round(time.time() - t0, 1), truncated=int(truncated), **usage_total)
 
 
def pick(conn, model: str, limit: int | None, commits: list[str]) -> list[str]:
    """Decisions later named by a Fixes: tag first (the leak-test cases), then oldest first."""
    if commits:
        return [conn.execute("SELECT commit_hash FROM commits WHERE commit_hash LIKE ?", (c + "%",)).fetchone()[0]
                for c in commits]
    rows = conn.execute("""
        SELECT ct.commit_hash FROM commit_times ct
        LEFT JOIN decision_records r ON r.commit_hash = ct.commit_hash AND r.model = ? AND r.prompt_version = ?
        WHERE r.commit_hash IS NULL
        ORDER BY EXISTS(SELECT 1 FROM fix_links f WHERE f.fixed_commit = ct.commit_hash) DESC, ct.applied_utc
        """, (model, PROMPT_VERSION)).fetchall()
    return [r[0] for r in rows][:limit] if limit else [r[0] for r in rows]
 
 
def run(conn, args) -> None:
    if args.backend == "ollama":
        backend = OllamaBackend(args.model, args.think, args.host)
    elif args.backend == "gemini":
        backend = GeminiBackend(args.model)
    else:
        backend = FakeBackend()
    model = "fake" if args.backend == "fake" else args.model + ("+think" if args.think and args.backend == "ollama" else "")
    anon = Anonymizer(conn)
    todo = pick(conn, model, args.limit, args.commit)
    print(f"{len(todo)} decisions to extract with {model} (prompt {PROMPT_VERSION})", flush=True)
    for i, h in enumerate(todo, 1):
        try:
            r = extract_one(backend, anon, conn, h, args.retries)
        except RuntimeError as e:
            raise SystemExit(f"ERROR: {e}")
        conn.execute("""INSERT OR REPLACE INTO decision_records(commit_hash, model, prompt_version, record_json,
            kept, rejected, attempts, seconds, prompt_tokens, output_tokens, truncated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (h, model, PROMPT_VERSION, json.dumps(r["record"]), r["kept"], r["rejected"], r["attempts"],
             r["seconds"], r["prompt_tokens"], r["output_tokens"], r["truncated"]))
        conn.commit()
        subject = conn.execute("SELECT subject FROM commits WHERE commit_hash=?", (h,)).fetchone()[0]
        print(f"  [{i}/{len(todo)}] {h[:12]} kept {r['kept']}, rejected {r['rejected']}, "
              f"tries {r['attempts']}, {r['seconds']}s  {subject[:50]}", flush=True)
 
 
def review(conn, args) -> None:
    rows = conn.execute("""SELECT r.commit_hash, r.record_json, r.kept, r.rejected FROM decision_records r
        WHERE r.prompt_version = ? AND (? IS NULL OR r.model = ?)
          AND (? = 0 OR r.commit_hash IN (SELECT commit_hash FROM decision_records WHERE prompt_version != ?))
        ORDER BY r.created_at LIMIT ?""",
        (args.prompt_version, args.model_filter, args.model_filter, int(args.compared), args.prompt_version,
         args.limit or 5)).fetchall()
    for h, rec, kept, rejected in rows:
        text, _, _ = message_for(conn, h)
        rec = json.loads(rec)
        print("=" * 90 + f"\n{h[:12]}   kind: {rec['kind']}   rationale stated: {rec['rationale_stated']}"
              f"   kept {kept}, rejected {rejected}\n" + "-" * 90)
        print(text.strip()[:1500] + "\n" + "-" * 90)
        for k in LISTS:
            for item in rec.get(k, []):
                print(f"{k.upper():13} {item['point']}\n{'':13} \"{item['quote']}\"")
        print()
 
 
def stats(conn, args) -> None:
    rows = conn.execute("""SELECT model, record_json, kept, rejected, attempts, seconds, output_tokens
        FROM decision_records WHERE prompt_version = ?""", (args.prompt_version,)).fetchall()
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r[0], []).append(r)
    for model, rs in by.items():
        n = len(rs)
        kept, rej = sum(r[2] for r in rs), sum(r[3] for r in rs)
        recs = [json.loads(r[1]) for r in rs]
        fields = "  ".join(f"{k} {sum(len(x.get(k, [])) for x in recs)}" for k in LISTS)
        print(f"{model} (prompt {args.prompt_version}): {n} decisions | quotes kept {kept} of {kept + rej} "
              f"({100 * kept / max(kept + rej, 1):.0f}%) | needed retry {sum(r[4] > 1 for r in rs)} | "
              f"duplicates dropped {sum(x.get('duplicates_dropped', 0) for x in recs)} | "
              f"rationale stated {sum(bool(x.get('rationale_stated')) for x in recs)} | "
              f"{sum(r[5] or 0 for r in rs) / n:.1f}s, {sum(r[6] or 0 for r in rs) / n:.0f} output tokens each")
        print(f"   items per field: {fields}")
 
 
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["run", "review", "stats"])
    p.add_argument("--db", default="data/db/linux.db")
    p.add_argument("--backend", choices=["ollama", "gemini", "fake"], default="ollama")
    p.add_argument("--model", default="gemma4:e4b")
    p.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    p.add_argument("--think", action="store_true", help="let the model think first (slower)")
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--limit", type=int)
    p.add_argument("--commit", action="append", default=[], help="specific hash or prefix; repeatable")
    p.add_argument("--model-filter", help="review only this model's records")
    p.add_argument("--prompt-version", default=PROMPT_VERSION, help="review/stats an older prompt, e.g. v1")
    p.add_argument("--compared", action="store_true",
                   help="review only decisions that also have a record under another prompt version")
    a = p.parse_args()
    if not a.host.startswith("http"):
        a.host = "http://" + a.host
    conn = sqlite3.connect(a.db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(RECORD_SCHEMA)
    {"run": run, "review": review, "stats": stats}[a.action](conn, a)
 
 
if __name__ == "__main__":
    main()
 
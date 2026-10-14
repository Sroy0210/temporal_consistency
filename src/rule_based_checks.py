from __future__ import annotations

import sqlite3
import re

_WS = re.compile(r"\s+")
 
def _norm(s: str) -> str:
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return _WS.sub(" ", s).strip()
 
 
def verify_quote(quote: str, source: str, min_len: int = 12) -> bool:
    q = _norm(quote)
    return len(q) >= min_len and q in _norm(source)

class Anonymizer:
    """Anonymizing the real persons and emails in the ledger database."""

    def __init__(self, conn: sqlite3.Connection):
        self._URL = re.compile(r"https?://\S+")
        self._EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
        self.fwd: dict[str, str] = {}      
        self.back: dict[str, str] = {}     
        rows = conn.execute("SELECT actor_id, email, name FROM actors ORDER BY actor_id").fetchall()
        for actor_id, email, name in rows:
            tag = f"P{actor_id:03d}"
            self.back[tag] = name or email
            self.fwd[email] = tag
            if name and len(name) >= 4:
                self.fwd[name] = tag
        keys = sorted(self.fwd, key=len, reverse=True)
        self._pat = re.compile("|".join(re.escape(k) for k in keys), re.I) if keys else None
        self._lookup = {k.lower(): v for k, v in self.fwd.items()}

    def hide(self, text: str) -> str:
        text = self._URL.sub(lambda m:self._tag(m.group(0), "LINK"), text)
        if self._pat:
            text = self._pat.sub(lambda m: self._lookup[m.group(0).lower()], text)
        return self._EMAIL.sub(lambda m:self._tag(m.group(0).lower(), "EMAIL"), text)

    def _tag(self, value: str, kind: str) -> str:
        if value not in self.fwd:
            n = sum(1 for k in self.back if k.startswith(kind)) + 1
            tag = f"{kind}{n:03d}"
            self.fwd[value], self.back[tag] = tag, value
        return self.fwd[value]
 
    def show(self, text: str) -> str:
         return re.sub(r"(?:P|LINK|EMAIL)\d{3}", lambda m: self.back.get(m.group(0), m.group(0)), text)

def leak_terms(conn: sqlite3.Connection, decision: str, t: int) -> list[str]:
    """Strings that only exist after the decision: later fixes' hashes and subjects."""
    terms = []
    for h, subject in conn.execute("""
            SELECT c.commit_hash, c.subject FROM fix_links f
            JOIN commits c ON c.commit_hash = f.fixing_commit
            JOIN commit_times ct ON ct.commit_hash = c.commit_hash
            WHERE f.fixed_commit = ? AND ct.applied_utc > ?""", (decision, t)):
        terms.append(h[:12])
        terms.append(re.sub(r"^\S+:\s*", "", subject).lower())   # drop the "ext4:" prefix
    return terms


def contains_leak(answer: str, terms: list[str]) -> list[str]:
    a = _norm(answer).lower()
    return [t for t in terms if t and t.lower() in a]

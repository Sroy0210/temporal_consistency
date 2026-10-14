from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

LEDGER_SCHEMA = """
DROP TABLE IF EXISTS participation;
DROP TABLE IF EXISTS commit_times;
DROP TABLE IF EXISTS actors;
CREATE TABLE actors (
 actor_id INTEGER PRIMARY KEY,
 email TEXT UNIQUE NOT NULL,
 name TEXT                         -- most frequent name seen for this email
);
CREATE TABLE commit_times (
 commit_hash TEXT PRIMARY KEY REFERENCES commits(commit_hash) ON DELETE CASCADE,
 authored_utc INTEGER NOT NULL,    -- patch written (seen by participants)
 applied_utc INTEGER NOT NULL      -- patch applied (the decision; public from here)
);
CREATE TABLE participation (
 commit_hash TEXT NOT NULL REFERENCES commits(commit_hash) ON DELETE CASCADE,
 actor_id INTEGER NOT NULL REFERENCES actors(actor_id),
 role TEXT NOT NULL,               -- author, committer, reviewed_by, acked_by, ...
 PRIMARY KEY (commit_hash, actor_id, role)
);
CREATE INDEX idx_part_actor ON participation(actor_id);
CREATE INDEX idx_times_applied ON commit_times(applied_utc);
"""
 
# Trailers that mean "this person saw the patch before it was applied".
PARTICIPANT_KINDS = ("reviewed_by", "acked_by", "tested_by", "reported_by", "suggested_by",
                     "co_developed_by", "signed_off_by", "reported_and_tested_by", "cc")
 
 
def utc(ts: str) -> int:
    """ISO 8601 with offset -> seconds since epoch (UTC)."""
    return int(datetime.fromisoformat(ts).timestamp())
 
 
def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
 
 
# ----------------------------------------------------------------- build
 
def build(conn: sqlite3.Connection) -> None:
    conn.executescript(LEDGER_SCHEMA)
    commits = conn.execute("""
        SELECT commit_hash, author_name, author_email, author_timestamp,
               committer_name, committer_email, committer_timestamp
        FROM commits WHERE is_shallow_boundary = 0""").fetchall()
 
    names: dict[str, dict[str, int]] = {}
    def see(email, name):
        if not email:
            return None
        email = email.strip().lower()
        names.setdefault(email, {})
        if name:
            names[email][name] = names[email].get(name, 0) + 1
        return email
 
    links = []                                   # (commit, email, role)
    for h, an, ae, at, cn, ce, ct in commits:
        conn.execute("INSERT INTO commit_times VALUES (?,?,?)", (h, utc(at), utc(ct)))
        links.append((h, see(ae, an), "author"))
        links.append((h, see(ce, cn), "committer"))
 
    for h, kind, name, email in conn.execute(f"""
            SELECT t.commit_hash, t.kind, t.person_name, t.person_email FROM trailers t
            JOIN commit_times ct ON ct.commit_hash = t.commit_hash
            WHERE t.person_email IS NOT NULL
              AND t.kind IN ({','.join('?' * len(PARTICIPANT_KINDS))})""", PARTICIPANT_KINDS):
        links.append((h, see(email, name), kind))
 
    for email, counts in names.items():
        best = max(counts, key=counts.get) if counts else None
        conn.execute("INSERT INTO actors(email, name) VALUES (?,?)", (email, best))
    ids = dict(conn.execute("SELECT email, actor_id FROM actors"))
    conn.executemany("INSERT OR IGNORE INTO participation VALUES (?,?,?)",
                     [(h, ids[e], r) for h, e, r in links if e])
    conn.commit()
 
    n = lambda q: conn.execute(q).fetchone()[0]
    print(f"decisions {n('SELECT COUNT(*) FROM commit_times')}, "
          f"actors {n('SELECT COUNT(*) FROM actors')}, "
          f"participation rows {n('SELECT COUNT(*) FROM participation')}")

# ----------------------------------------------------------------- queries
 
def actor_id(conn, email: str) -> int | None:
    row = conn.execute("SELECT actor_id FROM actors WHERE email = ?", (email.lower(),)).fetchone()
    return row[0] if row else None

def info_set(conn, actor: int, t: int, mode: str = "knew", exclude: str | None = None) -> list[tuple]:
    sql = """
      SELECT c.commit_hash, ct.authored_utc, ct.applied_utc, c.subject,
             GROUP_CONCAT(DISTINCT p.role) AS roles,
             MIN(CASE WHEN p.role = 'author' THEN ct.authored_utc ELSE ct.applied_utc END) AS seen
      FROM commit_times ct
      JOIN commits c USING (commit_hash)
      LEFT JOIN participation p ON p.commit_hash = ct.commit_hash AND p.actor_id = :a
      WHERE ct.commit_hash != COALESCE(:x, '')
      GROUP BY c.commit_hash
      HAVING {cond}
      ORDER BY seen"""
    cond = "COUNT(p.actor_id) > 0 AND seen <= :t" if mode == "knew" else "seen <= :t"
    return conn.execute(sql.format(cond=cond), {"a": actor, "t": t, "x": exclude}).fetchall()

def decision(conn, commit: str) -> dict:
    row = conn.execute("""
        SELECT c.commit_hash, c.subject, c.committer_email, ct.authored_utc, ct.applied_utc
        FROM commits c JOIN commit_times ct USING (commit_hash)
        WHERE c.commit_hash LIKE ?""", (commit + "%",)).fetchall()
    if len(row) != 1:
        raise SystemExit(f"{'No' if not row else 'Ambiguous'} commit matches {commit!r}")
    h, subject, committer, authored, applied = row[0]
    people = conn.execute("""
        SELECT a.name, a.email, GROUP_CONCAT(p.role) FROM participation p
        JOIN actors a USING (actor_id) WHERE p.commit_hash = ? GROUP BY a.actor_id""", (h,)).fetchall()
    return dict(hash=h, subject=subject, decider=actor_id(conn, committer),
                decider_email=committer, authored=authored, applied=applied, people=people)
 
 
def files_of(conn, commit: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT new_path FROM file_changes WHERE commit_hash = ? AND in_scope = 1 "
        "AND new_path IS NOT NULL", (commit,))]
 
 
def hindsight(conn, commit: str, t: int) -> dict:
    fixes = conn.execute("""
        SELECT c.commit_hash, ct.applied_utc, c.subject FROM fix_links f
        JOIN commits c ON c.commit_hash = f.fixing_commit
        JOIN commit_times ct ON ct.commit_hash = c.commit_hash
        WHERE f.fixed_commit = ? ORDER BY ct.applied_utc""", (commit,)).fetchall()
    files = files_of(conn, commit)
    later = []
    if files:
        later = conn.execute(f"""
            SELECT DISTINCT c.commit_hash, ct.applied_utc, c.subject FROM file_changes fc
            JOIN commits c USING (commit_hash) JOIN commit_times ct USING (commit_hash)
            WHERE fc.in_scope = 1 AND fc.new_path IN ({','.join('?' * len(files))})
              AND ct.applied_utc > ? AND c.commit_hash != ?
            ORDER BY ct.applied_utc LIMIT 10""", (*files, t, commit)).fetchall()
    return dict(fixes=fixes, later_same_files=later)

# ----------------------------------------------------------------- CLI

def inspect(conn, commit: str) -> None:
    d = decision(conn, commit)
    t = d["applied"]
    print(f"\nDECISION  {d['hash'][:12]}  {d['subject']}")
    print(f"  written {iso(d['authored'])}   applied {iso(t)}   decider {d['decider_email']}")
    print("  people on this patch:")
    for name, email, roles in d["people"]:
        print(f"    {name or '?':28} {roles}")
 
    knew = info_set(conn, d["decider"], t, "knew", exclude=d["hash"])
    could = info_set(conn, d["decider"], t, "could", exclude=d["hash"])
    print(f"\n  AT THE TIME - decider took part in {len(knew)} earlier patches "
          f"({len(could)} were public). Most recent 5 they took part in:")
    for h, a, ap, s, roles, seen in knew[-5:]:
        print(f"    seen {iso(seen)}  {h[:12]}  {s[:50]}  [{roles}]")
 
    hs = hindsight(conn, d["hash"], t)
    print(f"\n  WITH HINDSIGHT - {len(hs['fixes'])} later fix(es) name this patch:")
    for h, ap, s in hs["fixes"]:
        print(f"    {iso(ap)}  {h[:12]}  {s[:60]}")
    print(f"  later patches to the same files: {len(hs['later_same_files'])} (first 3)")
    for h, ap, s in hs["later_same_files"][:3]:
        print(f"    {iso(ap)}  {h[:12]}  {s[:60]}")

def check(conn) -> None:
    decisions = conn.execute("""SELECT c.commit_hash, ct.applied_utc, a.actor_id FROM commits c
        JOIN commit_times ct USING (commit_hash) JOIN actors a ON a.email = lower(c.committer_email)""").fetchall()
    roles_of = {}
    for h, a, r in conn.execute("SELECT commit_hash, actor_id, role FROM participation"):
        roles_of.setdefault((h, a), set()).add(r)
    leaks = 0
    for h, t, actor in decisions:
        for mode in ("knew", "could"):
            for row_hash, authored, applied, _, _, _ in info_set(conn, actor, t, mode, exclude=h):
                roles = roles_of.get((row_hash, actor), set())
                if mode == "knew" and not roles:
                    leaks += 1
                seen = authored if "author" in roles else applied
                if seen > t:
                    leaks += 1
                    print(f"LEAK {mode}: decision {h[:12]} saw {row_hash[:12]}", file=sys.stderr)
    later_fixes = conn.execute("SELECT COUNT(*) FROM fix_links WHERE fixed_commit IS NOT NULL").fetchone()[0]
    print(f"checked {len(decisions)} decisions x 2 modes: {leaks} leaks; "
          f"{later_fixes} decisions have a later fix (future-leak test cases)")

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["build", "inspect", "check"])
    p.add_argument("--db", type=Path, default=Path("data/db/linux.db"))
    p.add_argument("--commit", help="hash or prefix to inspect")
    a = p.parse_args()
    conn = sqlite3.connect(a.db)
    conn.execute("PRAGMA foreign_keys=ON")
    if a.action == "build":
        build(conn)
    elif a.action == "inspect":
        if not a.commit:
            raise sys.exit("Please specify a commit hash or prefix with --commit")
        inspect(conn, a.commit)
    else:
        check(conn)


if __name__ == "__main__":
    main()
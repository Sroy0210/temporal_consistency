#!/usr/bin/env python3
"""Ingest Linux Git history into local SQLite. No API or cloud calls.

Two modes:
  first-parent  main-line commits only (squash-merge repos such as FastAPI)
  no-merges     every non-merge commit, usually with --path (the Linux kernel)

Examples:
  python ingest_linux.py --repo data/raw/linux --mode no-merges --since 2025-11-01 --path fs/ext4 --db data/db/linux.db --reset
  python ingest_linux.py --repo data/raw/fastapi --mode first-parent --since 2026-01-01
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS repositories (
 repo_id INTEGER PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL,
 scope TEXT NOT NULL,            -- mode + since/until + paths, as JSON
 branch TEXT, head_commit TEXT, ingested_at TEXT DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(path, scope)
);
CREATE TABLE IF NOT EXISTS commits (
 commit_hash TEXT PRIMARY KEY,
 repo_id INTEGER REFERENCES repositories(repo_id),
 seq INTEGER,                    -- oldest-first order within this ingest scope
 parent_hash TEXT,               -- base used for file changes and diff
 parent_hashes_json TEXT NOT NULL,
 author_name TEXT, author_email TEXT, author_timestamp TEXT,
 committer_name TEXT, committer_email TEXT, committer_timestamp TEXT,
 subject TEXT, body TEXT,
 is_merge INTEGER NOT NULL DEFAULT 0,
 is_shallow_boundary INTEGER NOT NULL DEFAULT 0,  -- parents cut off by a shallow clone
 is_bot INTEGER NOT NULL DEFAULT 0,
 is_revert INTEGER NOT NULL DEFAULT 0,
 reverts_hash TEXT,              -- from "This reverts commit <hash>"
 pr_number INTEGER               -- from a trailing "(#123)" in the subject
);
CREATE INDEX IF NOT EXISTS idx_commits_seq ON commits(repo_id, seq);
CREATE INDEX IF NOT EXISTS idx_commits_ctime ON commits(committer_timestamp);
CREATE INDEX IF NOT EXISTS idx_commits_pr ON commits(pr_number);
CREATE TABLE IF NOT EXISTS trailers (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 commit_hash TEXT NOT NULL REFERENCES commits(commit_hash) ON DELETE CASCADE,
 position INTEGER NOT NULL,      -- order within the commit's trailer block
 kind TEXT NOT NULL,             -- normalized key, e.g. reviewed_by, fixes, link
 raw_key TEXT NOT NULL,
 value TEXT NOT NULL,
 person_name TEXT, person_email TEXT,   -- for Name <email> trailers
 ref TEXT                        -- hash for fixes, URL for link/closes
);
CREATE INDEX IF NOT EXISTS idx_trailers_commit ON trailers(commit_hash);
CREATE INDEX IF NOT EXISTS idx_trailers_kind ON trailers(kind);
CREATE INDEX IF NOT EXISTS idx_trailers_email ON trailers(person_email);
CREATE TABLE IF NOT EXISTS fix_links (
 fixing_commit TEXT NOT NULL REFERENCES commits(commit_hash) ON DELETE CASCADE,
 fixed_prefix TEXT NOT NULL,     -- hash as written in the Fixes: tag
 fixed_subject TEXT,             -- subject as quoted in the Fixes: tag
 fixed_commit TEXT,              -- full hash when the fixed commit is in the DB
 PRIMARY KEY (fixing_commit, fixed_prefix)
);
CREATE TABLE IF NOT EXISTS file_changes (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 commit_hash TEXT NOT NULL REFERENCES commits(commit_hash) ON DELETE CASCADE,
 old_path TEXT, new_path TEXT, change_type TEXT NOT NULL,
 additions INTEGER, deletions INTEGER, binary_or_unavailable INTEGER DEFAULT 0,
 in_scope INTEGER NOT NULL DEFAULT 1   -- under one of the --path filters
);
CREATE INDEX IF NOT EXISTS idx_changes_commit ON file_changes(commit_hash);
CREATE INDEX IF NOT EXISTS idx_changes_path ON file_changes(new_path);
CREATE TABLE IF NOT EXISTS diffs (
 commit_hash TEXT PRIMARY KEY REFERENCES commits(commit_hash) ON DELETE CASCADE,
 diff_text TEXT NOT NULL, truncated INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ingestion_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT DEFAULT CURRENT_TIMESTAMP,
 finished_at TEXT, repo_path TEXT NOT NULL, scope TEXT, head_commit TEXT,
 commit_count INTEGER DEFAULT 0, file_change_count INTEGER DEFAULT 0,
 trailer_count INTEGER DEFAULT 0, status TEXT NOT NULL, error TEXT
);
"""

FS, RS, GS = "\x1f", "\x1e", "\x1d"   # field, record, trailer separators
PR_RE = re.compile(r"\(#(\d+)\)\s*$")
REVERT_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})", re.I)
PERSON_RE = re.compile(r"^(?P<name>.*?)\s*<(?P<email>[^<>\s]+@[^<>\s]+)>")
FIXES_RE = re.compile(r"^(?P<hash>[0-9a-f]{8,40})\b(?:\s*\(\"?(?P<subject>.*?)\"?\))?", re.I)
URL_RE = re.compile(r"https?://\S+")
BOT_HINTS = ("[bot]", "github-actions", "dependabot", "renovate", "pre-commit-ci")
PERSON_KINDS = {"signed_off_by", "reviewed_by", "acked_by", "tested_by", "reported_by",
                "suggested_by", "co_developed_by", "cc", "reviewed_and_tested_by"}
URL_KINDS = {"link", "closes", "bugzilla", "message_id"}


def git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args[:4])} failed: {p.stderr.strip()}")
    return p.stdout


def validate_repo(repo: Path) -> Path:
    repo = repo.expanduser().resolve()
    if not repo.exists() or not git(repo, "rev-parse", "--git-dir", check=False).strip():
        raise ValueError(f"Not a Git repository: {repo}")
    return repo


# ---------------------------------------------------------------- commits

def log_args(mode: str, since: str | None, until: str | None, paths: list[str]) -> list[str]:
    args = ["log", "--reverse"]
    args.append("--first-parent" if mode == "first-parent" else "--no-merges")
    if paths and mode == "no-merges":
        # Without this, path-limited history simplification hides commits that
        # reached the main line through merges (most kernel patches).
        args.append("--full-history")
    if since: args.append(f"--since={since}")
    if until: args.append(f"--until={until}")
    args.append("HEAD")
    if paths: args += ["--", *paths]
    return args


def shallow_hashes(repo: Path) -> set[str]:
    f = Path(git(repo, "rev-parse", "--git-path", "shallow").strip())
    f = f if f.is_absolute() else repo / f
    return set(f.read_text().split()) if f.exists() else set()


def read_commits(repo: Path, mode: str, since, until, paths) -> list[dict]:
    shallow = shallow_hashes(repo)
    fmt = FS.join(["%H", "%P", "%an", "%ae", "%aI", "%cn", "%ce", "%cI", "%s", "%b",
                   f"%(trailers:unfold=true,separator=%x1d)"]) + RS
    args = log_args(mode, since, until, paths)
    args.insert(2, f"--format={fmt}")
    raw = git(repo, *args)
    out = []
    for record in raw.split(RS):
        if not record.strip():
            continue
        f = record.strip("\n").split(FS)
        if len(f) != 11:
            print(f"Skipping malformed record: {f[:1]}", file=sys.stderr)
            continue
        h, parents, an, ae, at, cn, ce, ct, subject, body, trailer_blob = f
        plist = parents.split()
        rev = REVERT_RE.search(body)
        pr = PR_RE.search(subject)
        out.append(dict(
            commit_hash=h, seq=len(out), parent_hash=plist[0] if plist else None,
            parent_hashes_json=json.dumps(plist),
            author_name=an, author_email=ae, author_timestamp=at,
            committer_name=cn, committer_email=ce, committer_timestamp=ct,
            subject=subject, body=body.strip(),
            is_merge=int(len(plist) > 1),
            is_shallow_boundary=int(not plist and h in shallow),
            is_bot=int(any(b in f"{an} {ae}".lower() for b in BOT_HINTS)),
            is_revert=int(bool(rev) or subject.startswith(("Revert \"", "⏪"))),
            reverts_hash=rev.group(1).lower() if rev else None,
            pr_number=int(pr.group(1)) if pr else None,
            _trailers=parse_trailers(trailer_blob)))
    return out


def parse_trailers(blob: str) -> list[dict]:
    rows = []
    for pos, item in enumerate(t for t in blob.split(GS) if t.strip()):
        key, sep, value = item.strip().partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        kind = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        row = dict(position=pos, kind=kind, raw_key=key, value=value,
                   person_name=None, person_email=None, ref=None)
        if kind in PERSON_KINDS or PERSON_RE.match(value):
            m = PERSON_RE.match(value)
            if m:
                row["person_name"] = m.group("name").strip().strip('"') or None
                row["person_email"] = m.group("email").lower()
        if kind == "fixes":
            m = FIXES_RE.match(value)
            if m:
                row["ref"] = m.group("hash").lower()
                row["_fixed_subject"] = m.group("subject")
        elif kind in URL_KINDS:
            m = URL_RE.search(value)
            row["ref"] = m.group(0).rstrip(">.,)") if m else value
        rows.append(row)
    return rows


# ------------------------------------------------------- files and diffs

def in_scope(path: str | None, paths: list[str]) -> int:
    if not paths or not path:
        return 1
    return int(any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in paths))


def file_changes(repo: Path, c: dict, paths: list[str]) -> list[dict]:
    h, base = c["commit_hash"], c["parent_hash"]
    target = [base, h] if base else ["--root", h]
    names = git(repo, "diff-tree", "--no-commit-id", "-r", "-M", "-z", "--name-status", *target)
    nums = git(repo, "diff-tree", "--no-commit-id", "-r", "-M", "-z", "--numstat", *target)

    # --numstat -z: "A\tD\tpath\0" or, for renames/copies, "A\tD\t\0old\0new\0"
    stats, tok, i = {}, nums.split("\0"), 0
    while i < len(tok):
        t = tok[i]; i += 1
        if not t:
            continue
        a, d, path = (t.split("\t") + ["", ""])[:3]
        if path == "":                       # rename: old and new follow
            path = tok[i + 1] if i + 1 < len(tok) else ""
            i += 2
        binary = a == "-" or d == "-"
        stats[path] = (None if binary else int(a), None if binary else int(d), binary)

    # --name-status -z: "M\0path\0" or "R100\0old\0new\0"
    rows, tok, i = [], names.split("\0"), 0
    while i < len(tok):
        code = tok[i]; i += 1
        if not code:
            continue
        kind = code[0]
        if kind in ("R", "C"):
            old, new = tok[i], tok[i + 1]; i += 2
        else:
            p = tok[i]; i += 1
            old, new = (None, p) if kind == "A" else (p, None) if kind == "D" else (p, p)
        a, d, binary = stats.get(new or old, (None, None, False))
        rows.append(dict(commit_hash=h, old_path=old, new_path=new, change_type=kind,
                         additions=a, deletions=d, binary_or_unavailable=int(binary),
                         in_scope=max(in_scope(new, paths), in_scope(old, paths))))
    return rows


def get_diff(repo: Path, c: dict, limit: int):
    h, base = c["commit_hash"], c["parent_hash"]
    target = [base, h] if base else ["--root", h]
    text = git(repo, "diff-tree", "-p", "--no-commit-id", "-r", "-M", "--no-color",
               "--unified=3", "--no-ext-diff", *target)
    truncated = int(len(text) > limit)
    if truncated:
        text = text[:limit] + "\n[DIFF TRUNCATED BY INGESTION LIMIT]\n"
    return text, truncated


# ---------------------------------------------------------------- ingest

def resolve_fix_links(conn: sqlite3.Connection) -> int:
    """Point each Fixes: prefix at a full hash when that commit is in the DB."""
    rows = conn.execute("SELECT fixing_commit, fixed_prefix FROM fix_links WHERE fixed_commit IS NULL").fetchall()
    hit = 0
    for fixing, prefix in rows:
        m = conn.execute("SELECT commit_hash FROM commits WHERE commit_hash LIKE ? LIMIT 2",
                         (prefix + "%",)).fetchall()
        if len(m) == 1:
            conn.execute("UPDATE fix_links SET fixed_commit=? WHERE fixing_commit=? AND fixed_prefix=?",
                         (m[0][0], fixing, prefix))
            hit += 1
    return hit


SCHEMA_VERSION = 2
TABLES = ["diffs", "file_changes", "fix_links", "trailers", "commits", "repositories", "ingestion_runs"]


def prepare_schema(conn: sqlite3.Connection, reset: bool) -> None:
    """Create the schema, or stop clearly if the DB was made by an older ingester."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    has_tables = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='commits'").fetchone()[0]
    if has_tables and version != SCHEMA_VERSION:
        if not reset:
            raise RuntimeError(
                "This database was created by an older version of the ingester (different "
                "columns). Use a new --db path, or add --reset to drop and rebuild its tables.")
        conn.execute("PRAGMA foreign_keys=OFF")
        for t in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()
        print("Dropped tables from the older schema.")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def ingest(repo, db, mode, since, until, paths, max_diff_chars, with_diffs, limit, reset=False):
    repo = validate_repo(repo)
    db = db.expanduser().resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    scope = json.dumps({"mode": mode, "since": since, "until": until, "paths": paths})
    head = git(repo, "rev-parse", "HEAD").strip()
    branch = git(repo, "branch", "--show-current").strip() or None

    commits = read_commits(repo, mode, since, until, paths)
    if limit:
        commits = commits[:limit]
    if not commits:
        raise RuntimeError("No commits matched the scope")
    print(f"{len(commits)} commits in scope {scope}")

    conn = sqlite3.connect(db)
    run_id = None
    try:
        prepare_schema(conn, reset)
        run_id = conn.execute(
            "INSERT INTO ingestion_runs(repo_path, scope, head_commit, status) VALUES (?,?,?,'running')",
            (str(repo), scope, head)).lastrowid
        conn.execute("""INSERT INTO repositories(path,name,scope,branch,head_commit) VALUES (?,?,?,?,?)
            ON CONFLICT(path, scope) DO UPDATE SET branch=excluded.branch,
            head_commit=excluded.head_commit, ingested_at=CURRENT_TIMESTAMP""",
            (str(repo), repo.name, scope, branch, head))
        repo_id = conn.execute("SELECT repo_id FROM repositories WHERE path=? AND scope=?",
                               (str(repo), scope)).fetchone()[0]

        n_files = n_trailers = skipped = 0
        for i, c in enumerate(commits, 1):
            h = c["commit_hash"]
            for t in ("trailers", "fix_links", "file_changes", "diffs"):
                conn.execute(f"DELETE FROM {t} WHERE {'fixing_commit' if t == 'fix_links' else 'commit_hash'}=?", (h,))
            conn.execute("""INSERT OR REPLACE INTO commits
              (commit_hash,repo_id,seq,parent_hash,parent_hashes_json,author_name,author_email,
               author_timestamp,committer_name,committer_email,committer_timestamp,subject,body,
               is_merge,is_shallow_boundary,is_bot,is_revert,reverts_hash,pr_number)
              VALUES (:commit_hash,:repo_id,:seq,:parent_hash,:parent_hashes_json,:author_name,
               :author_email,:author_timestamp,:committer_name,:committer_email,:committer_timestamp,
               :subject,:body,:is_merge,:is_shallow_boundary,:is_bot,:is_revert,:reverts_hash,:pr_number)""",
              {**c, "repo_id": repo_id})

            for t in c["_trailers"]:
                conn.execute("""INSERT INTO trailers
                  (commit_hash,position,kind,raw_key,value,person_name,person_email,ref)
                  VALUES (?,?,?,?,?,?,?,?)""",
                  (h, t["position"], t["kind"], t["raw_key"], t["value"],
                   t["person_name"], t["person_email"], t["ref"]))
                if t["kind"] == "fixes" and t["ref"]:
                    conn.execute("INSERT OR IGNORE INTO fix_links(fixing_commit,fixed_prefix,fixed_subject) VALUES (?,?,?)",
                                 (h, t["ref"], t.get("_fixed_subject")))
            n_trailers += len(c["_trailers"])

            if c["is_shallow_boundary"]:
                # Its parent was not downloaded; diffing it would list the whole tree.
                skipped += 1
                continue
            changes = file_changes(repo, c, paths)
            conn.executemany("""INSERT INTO file_changes
              (commit_hash,old_path,new_path,change_type,additions,deletions,binary_or_unavailable,in_scope)
              VALUES (:commit_hash,:old_path,:new_path,:change_type,:additions,:deletions,
               :binary_or_unavailable,:in_scope)""", changes)
            n_files += len(changes)

            if with_diffs:
                diff, truncated = get_diff(repo, c, max_diff_chars)
                conn.execute("INSERT INTO diffs(commit_hash,diff_text,truncated) VALUES (?,?,?)",
                             (h, diff, truncated))

            if i % 10 == 0:
                conn.commit()
            print(f"  [{i}/{len(commits)}] {h[:12]} {c['subject'][:60]}", flush=True)

        conn.commit()
        print(f"  {len(commits)}/{len(commits)} commits, {n_files} file changes, {n_trailers} trailers")
        if skipped:
            print(f"  WARNING: {skipped} commits sit on the shallow-clone boundary; their files and "
                  "diffs were skipped. Re-clone with --shallow-since a month before --since.",
                  file=sys.stderr)
        resolved = resolve_fix_links(conn)
        conn.execute("""UPDATE ingestion_runs SET finished_at=CURRENT_TIMESTAMP, commit_count=?,
          file_change_count=?, trailer_count=?, status='success' WHERE id=?""",
          (len(commits), n_files, n_trailers, run_id))
        conn.commit()
        print(f"Done: {db}\nHEAD {head}\nFixes: tags resolved to commits in the DB: {resolved}")
    except Exception as e:
        conn.rollback()
        if run_id:
            conn.execute("UPDATE ingestion_runs SET finished_at=CURRENT_TIMESTAMP,status='failed',error=? WHERE id=?",
                         (str(e), run_id))
            conn.commit()
        raise
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", type=Path, default=Path("data/raw/linux"))
    p.add_argument("--db", type=Path, default=Path("data/db/temporal.db"))
    p.add_argument("--mode", choices=["first-parent", "no-merges"], default="no-merges")
    p.add_argument("--since", help="e.g. 2026-01-01")
    p.add_argument("--until")
    p.add_argument("--path", action="append", default=[], help="repeatable, e.g. --path fs/ext4")
    p.add_argument("--max-diff-chars", type=int, default=250_000)
    p.add_argument("--no-diffs", action="store_true", help="skip diffs (fast; no blob downloads)")
    p.add_argument("--limit", type=int, help="ingest only the first N commits (testing)")
    p.add_argument("--reset", action="store_true",
                   help="drop tables left by an older ingester version, then rebuild")
    a = p.parse_args()
    try:
        ingest(a.repo, a.db, a.mode, a.since, a.until, a.path, a.max_diff_chars, not a.no_diffs, a.limit, a.reset)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
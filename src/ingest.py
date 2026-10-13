#!/usr/bin/env python3
"""Ingest first-parent Git history into local SQLite. No API/cloud calls."""
from __future__ import annotations
import argparse, json, sqlite3, subprocess, sys
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS repositories (
 repo_id INTEGER PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL,
 branch TEXT, head_commit TEXT, ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS commits (
 commit_hash TEXT PRIMARY KEY, first_parent_hash TEXT, parent_hashes_json TEXT NOT NULL,
 first_parent_position INTEGER UNIQUE, author_name TEXT, author_email TEXT,
 author_timestamp TEXT, committer_timestamp TEXT, subject TEXT, body TEXT,
 is_merge INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_commits_position ON commits(first_parent_position);
CREATE TABLE IF NOT EXISTS file_changes (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 commit_hash TEXT NOT NULL REFERENCES commits(commit_hash) ON DELETE CASCADE,
 old_path TEXT, new_path TEXT, change_type TEXT NOT NULL,
 additions INTEGER, deletions INTEGER, binary_or_unavailable INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_changes_commit ON file_changes(commit_hash);
CREATE TABLE IF NOT EXISTS diffs (
 commit_hash TEXT PRIMARY KEY REFERENCES commits(commit_hash) ON DELETE CASCADE,
 diff_text TEXT NOT NULL, truncated INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ingestion_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT DEFAULT CURRENT_TIMESTAMP,
 finished_at TEXT, repo_path TEXT NOT NULL, head_commit TEXT,
 commit_count INTEGER DEFAULT 0, file_change_count INTEGER DEFAULT 0,
 status TEXT NOT NULL, error TEXT
);
"""

def git(repo: Path, *args: str, check=True) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
        text=True, encoding="utf-8", errors="replace")
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout

def validate_repo(repo: Path) -> Path:
    repo = repo.expanduser().resolve()
    if not repo.exists() or not git(repo, "rev-parse", "--git-dir", check=False).strip():
        raise ValueError(f"Not a Git repository: {repo}")
    return repo

def read_commits(repo: Path) -> list[dict]:
    fmt = "%H%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%cI%x1f%s%x1f%b%x1e"
    raw = git(repo, "log", "--first-parent", "--reverse", "--format=" + fmt, "HEAD")
    result = []
    for record in raw.split("\x1e"):
        if not record.strip():
            continue
        fields = record.strip("\n").split("\x1f")
        if len(fields) != 8:
            print(f"Skipping malformed commit record: {fields[:2]}", file=sys.stderr)
            continue
        h, parents, author, email, authored, committed, subject, body = fields
        parent_list = parents.split() if parents.strip() else []
        result.append(dict(commit_hash=h, first_parent_hash=parent_list[0] if parent_list else None,
            parent_hashes_json=json.dumps(parent_list), first_parent_position=len(result),
            author_name=author, author_email=email, author_timestamp=authored,
            committer_timestamp=committed, subject=subject, body=body.strip(),
            is_merge=int(len(parent_list) > 1)))
    return result

def parse_name_status(line: str):
    parts = line.split("\t")
    code = parts[0][0]
    if code in ("R", "C") and len(parts) >= 3:
        return code, parts[1], parts[2]
    path = parts[1] if len(parts) > 1 else None
    if code == "A": return code, None, path
    if code == "D": return code, path, None
    return code, path, path

def file_changes(repo: Path, commit: dict) -> list[dict]:
    h, parent = commit["commit_hash"], commit["first_parent_hash"]
    if parent:
        names = git(repo, "diff", "--no-ext-diff", "--name-status", "-M", parent, h)
        nums = git(repo, "diff", "--no-ext-diff", "--numstat", "-M", parent, h)
    else:
        names = git(repo, "diff-tree", "--root", "--no-commit-id", "--name-status", "-r", "-M", h)
        nums = git(repo, "diff-tree", "--root", "--no-commit-id", "--numstat", "-r", "-M", h)
    stats = {}
    for line in nums.splitlines():
        p = line.split("\t")
        if len(p) == 3:
            a, d, path = p
            stats[path] = (None, None, True) if "-" in (a, d) else (int(a), int(d), False)
    rows = []
    for line in names.splitlines():
        if not line: continue
        kind, old, new = parse_name_status(line)
        stat = stats.get(new or old, (None, None, False))
        rows.append(dict(commit_hash=h, old_path=old, new_path=new, change_type=kind,
            additions=stat[0], deletions=stat[1], binary_or_unavailable=int(stat[2])))
    return rows

def get_diff(repo: Path, commit: dict, limit: int):
    h, parent = commit["commit_hash"], commit["first_parent_hash"]
    if parent:
        text = git(repo, "diff", "--no-ext-diff", "--no-color", "--find-renames",
                   "--unified=3", parent, h)
    else:
        text = git(repo, "show", "--format=", "--no-ext-diff", "--no-color",
                   "--root", "--find-renames", "--unified=3", h)
    truncated = int(len(text) > limit)
    if truncated:
        text = text[:limit] + "\n[DIFF TRUNCATED BY INGESTION LIMIT]\n"
    return text, truncated

def ingest(repo: Path, db: Path, max_diff_chars: int):
    repo = validate_repo(repo)
    db = db.expanduser().resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    head = git(repo, "rev-parse", "HEAD").strip()
    branch = git(repo, "branch", "--show-current").strip() or None
    commits = read_commits(repo)
    if not commits: raise RuntimeError("No commits found at HEAD")
    conn = sqlite3.connect(db)
    try:
        conn.executescript(SCHEMA)
        run_id = conn.execute(
            "INSERT INTO ingestion_runs(repo_path, head_commit, status) VALUES (?, ?, 'running')",
            (str(repo), head)).lastrowid
        conn.execute("INSERT INTO repositories(path,name,branch,head_commit) VALUES (?,?,?,?)",
                     (str(repo), repo.name, branch, head))
        conn.executemany("""INSERT OR REPLACE INTO commits
          (commit_hash,first_parent_hash,parent_hashes_json,first_parent_position,
           author_name,author_email,author_timestamp,committer_timestamp,subject,body,is_merge)
          VALUES (:commit_hash,:first_parent_hash,:parent_hashes_json,:first_parent_position,
           :author_name,:author_email,:author_timestamp,:committer_timestamp,:subject,:body,:is_merge)""",
          commits)
        conn.commit()
        total = 0
        for i, c in enumerate(commits, 1):
            h = c["commit_hash"]
            conn.execute("DELETE FROM file_changes WHERE commit_hash=?", (h,))
            conn.execute("DELETE FROM diffs WHERE commit_hash=?", (h,))
            changes = file_changes(repo, c)
            diff, truncated = get_diff(repo, c, max_diff_chars)
            conn.executemany("""INSERT INTO file_changes
              (commit_hash,old_path,new_path,change_type,additions,deletions,binary_or_unavailable)
              VALUES (:commit_hash,:old_path,:new_path,:change_type,:additions,:deletions,:binary_or_unavailable)""",
              changes)
            conn.execute("INSERT INTO diffs(commit_hash,diff_text,truncated) VALUES (?,?,?)",
                         (h, diff, truncated))
            total += len(changes)
            if i % 100 == 0 or i == len(commits):
                conn.commit()
                print(f"Ingested {i}/{len(commits)} commits; {total} file changes")
        conn.execute("""UPDATE ingestion_runs SET finished_at=CURRENT_TIMESTAMP,
          commit_count=?,file_change_count=?,status='success' WHERE id=?""", (len(commits),total,run_id))
        conn.commit()
        print(f"Done: {db}\nHEAD: {head}\nFirst-parent commits: {len(commits)}\nFile changes: {total}")
    except Exception as e:
        conn.rollback()
        try:
            conn.execute("UPDATE ingestion_runs SET finished_at=CURRENT_TIMESTAMP,status='failed',error=? WHERE id=?",
                         (str(e), locals().get("run_id")))
            conn.commit()
        except Exception: pass
        raise
    finally:
        conn.close()

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, default=Path("data/raw/fastapi"))
    p.add_argument("--db", type=Path, default=Path("data/db/temporal.db"))
    p.add_argument("--max-diff-chars", type=int, default=250000)
    a = p.parse_args()
    try: ingest(a.repo, a.db, a.max_diff_chars)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr); sys.exit(1)

if __name__ == "__main__": main()

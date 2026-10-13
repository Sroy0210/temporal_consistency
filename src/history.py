"""Helpers for reading a repository at a historical commit."""
import sqlite3, subprocess
from pathlib import Path

def get_commit_at_position(db_path, position):
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM commits WHERE first_parent_position=?", (position,)).fetchone()
        return dict(row) if row else None

def get_file_at_commit(repo_path, commit_hash, file_path):
    p = subprocess.run(["git", "-C", str(Path(repo_path).resolve()),
        "show", f"{commit_hash}:{file_path}"], capture_output=True,
        text=True, encoding="utf-8", errors="replace")
    return p.stdout if p.returncode == 0 else None

def get_changes_for_commit(db_path, commit_hash):
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""SELECT old_path,new_path,change_type,additions,deletions
          FROM file_changes WHERE commit_hash=? ORDER BY new_path,old_path""", (commit_hash,)).fetchall()
        return [dict(r) for r in rows]

def get_commit_diff(db_path, commit_hash):
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT diff_text,truncated FROM diffs WHERE commit_hash=?", (commit_hash,)).fetchone()
        return dict(r) if r else None

"""Export commit events from SQLite to JSON Lines."""
import argparse, json, sqlite3
from pathlib import Path

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/db/temporal.db")
    p.add_argument("--out", default="data/datasets/commit_events.jsonl")
    p.add_argument("--include-diff", action="store_true")
    a = p.parse_args()
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(a.db); conn.row_factory = sqlite3.Row
    count = 0
    try:
        rows = conn.execute("SELECT * FROM commits ORDER BY first_parent_position").fetchall()
        with out.open("w", encoding="utf-8") as f:
            for r in rows:
                item = dict(r)
                item["file_changes"] = [dict(x) for x in conn.execute(
                    """SELECT old_path,new_path,change_type,additions,deletions
                       FROM file_changes WHERE commit_hash=? ORDER BY new_path,old_path""",
                    (item["commit_hash"],))]
                if a.include_diff:
                    d = conn.execute("SELECT diff_text,truncated FROM diffs WHERE commit_hash=?",
                                     (item["commit_hash"],)).fetchone()
                    item["diff"] = dict(d) if d else None
                f.write(json.dumps(item, ensure_ascii=False) + "\n"); count += 1
    finally: conn.close()
    print(f"Exported {count} commits to {out.resolve()}")

if __name__ == "__main__": main()

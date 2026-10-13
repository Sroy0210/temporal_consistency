"""Print database integrity and ingestion counts."""
import argparse, sqlite3
def main():
    p = argparse.ArgumentParser(); p.add_argument("--db", default="data/db/temporal.db"); a = p.parse_args()
    with sqlite3.connect(a.db) as c:
        print("SQLite integrity:", c.execute("PRAGMA integrity_check").fetchone()[0])
        for t in ("repositories","commits","file_changes","diffs","ingestion_runs"):
            print(f"{t}: {c.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]}")
        print("\nEarliest/latest first-parent positions:")
        for r in c.execute("""SELECT first_parent_position,commit_hash,author_timestamp,subject
          FROM commits WHERE first_parent_position IN
          ((SELECT MIN(first_parent_position) FROM commits),
           (SELECT MAX(first_parent_position) FROM commits))
          ORDER BY first_parent_position"""):
            print(r)
        print("\nLatest run:", c.execute("SELECT status,error FROM ingestion_runs ORDER BY id DESC LIMIT 1").fetchone())
if __name__ == "__main__": main()

# Temporal Consistency — local ingestion

The ingestion pipeline uses Git, Python standard library, and SQLite. It makes no
cloud calls and requires no model API.

## Setup
```bash
git clone https://github.com/fastapi/fastapi.git data/raw/fastapi
python src/ingest.py --repo data/raw/fastapi --db data/db/temporal.db
python src/check_ingestion.py --db data/db/temporal.db
python src/export_jsonl.py --db data/db/temporal.db --out data/datasets/commit_events.jsonl
```

Include diffs in JSONL only if you need the larger output:
```bash
python src/export_jsonl.py --include-diff
```

Read an exact historical file:
```python
from src.history import get_file_at_commit
text = get_file_at_commit("data/raw/fastapi", "COMMIT_HASH", "fastapi/routing.py")
```

## Notes
- `first_parent_position` defines deterministic cutoff order for this first trial;
  timestamps are retained as metadata but aren't used as the sole ordering source.
- Full historical file contents are not duplicated into SQLite. Git reconstructs them
  on demand, avoiding a huge database.
- Each commit diff is capped at 250,000 characters by default; configure with
  `--max-diff-chars`.
- Start with FastAPI rather than Linux. Ingestion can take a while on a large history.
- Keep generated data out of version control and review repository licensing before
  redistributing source or diffs.

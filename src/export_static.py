#!/usr/bin/env python3
"""Export the site's data as static files, for hosting without a server (Firebase Hosting).

  python src/export_static.py                  # writes web/public/data/
  cd web && npm run build:static               # builds the site to read those files
  firebase deploy --only hosting

Every API response the site uses for prepared content is written once:
  data/status.json, data/decisions.json, data/results.json
  data/decision/<hash>.json                    one per decision
  data/answers/<hash>.json                     prepared answers for that decision: {question: {B3, B1}}

Typed questions need the live server (app.py); the static site offers prepared questions only.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

os.environ.setdefault("LIVE_BACKEND", "off")    # export never calls a model

import app  # noqa: E402  (reuses the backend's own functions, so the files match the API exactly)


def write(path: Path, data) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="web/public/data")
    a = p.parse_args()
    out = Path(a.out)
    total = 0

    status = app.status()
    status.update(live=False, live_model=None, static=True)
    total += write(out / "status.json", status)
    total += write(out / "decisions.json", app.decisions())
    total += write(out / "results.json", app.results())

    for row in app.INDEX:
        total += write(out / "decision" / f"{row['commit']}.json", app.decision_detail(row["commit"]))

    conn = sqlite3.connect(app.DB_PATH)
    n_answers = 0
    for commit, case in app.PREPARED_COMMITS.items():
        pairs = {}
        for q in case["questions"]:
            try:
                pairs[q] = {pol: app.answer_one(conn, commit, q, pol, "prefetch", "export") for pol in ("B3", "B1")}
                n_answers += 2
            except Exception as e:                    # a missing prepared answer: skip that question
                print(f"  skipped {commit[:12]} {q[:50]!r}: {getattr(e, 'detail', e)}")
        total += write(out / "answers" / f"{commit}.json", pairs)
    conn.close()

    print(f"wrote {len(app.INDEX)} decisions and {n_answers} prepared answers to {out} ({total / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()

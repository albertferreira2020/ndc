"""Persistent task queue and run history (SQLite)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .config import state_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'work',
  complexity TEXT NOT NULL DEFAULT 'M',
  depends_on TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'pending',
  failures INTEGER NOT NULL DEFAULT 0,
  risk TEXT NOT NULL DEFAULT 'low', verify_cmd TEXT, expect_red INTEGER NOT NULL DEFAULT 0,
  may_edit_tests INTEGER NOT NULL DEFAULT 0, last_report TEXT NOT NULL DEFAULT '', files_touched TEXT NOT NULL DEFAULT '[]',
  tier TEXT, model TEXT, notes TEXT NOT NULL DEFAULT '',
  created_at TEXT, started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS protected(
  path TEXT PRIMARY KEY, task_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL, complexity TEXT NOT NULL, kind TEXT NOT NULL, tier TEXT,
  ok INTEGER NOT NULL, deltas TEXT NOT NULL, at TEXT NOT NULL
);
"""
COMPLEXITIES = ("S", "M", "L", "XL")
KINDS = ("work", "plan", "test", "explore", "docs", "chore")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path=None) -> sqlite3.Connection:
    db = sqlite3.connect(path or state_dir() / "ndc.db")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def _migrate(db) -> None:
    """Databases made by older versions keep their queue: add the columns they lack."""
    cols = {r["name"] for r in db.execute("PRAGMA table_info(tasks)")}
    for name, ddl in (("may_edit_tests", "INTEGER NOT NULL DEFAULT 0"), ("last_report", "TEXT NOT NULL DEFAULT ''"),
                      ("files_touched", "TEXT NOT NULL DEFAULT '[]'")):
        if name not in cols:
            db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
    db.commit()


def add_task(db, title, description="", complexity="M", kind="work", depends_on=(),
             risk="low", verify_cmd=None, expect_red=False, commit=True, may_edit_tests=False):
    if complexity not in COMPLEXITIES:
        raise ValueError(f"complexity must be one of {COMPLEXITIES}")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if risk not in ("low", "high"):
        raise ValueError("risk must be 'low' or 'high'")
    if expect_red and not verify_cmd:
        raise ValueError("--expect-red needs --verify")
    known = {r["id"] for r in db.execute("SELECT id FROM tasks")}
    bad = [d for d in depends_on if d not in known]
    if bad:
        raise ValueError(f"unknown dependency ids: {bad}")
    cur = db.execute(
        "INSERT INTO tasks(title,description,kind,complexity,depends_on,risk,verify_cmd,expect_red,may_edit_tests,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (title, description, kind, complexity, json.dumps(list(depends_on)), risk, verify_cmd, int(expect_red),
         int(may_edit_tests), now()),
    )
    if commit:
        db.commit()
    return cur.lastrowid


def get(db, task_id):
    return db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def list_tasks(db, status=None):
    q = "SELECT * FROM tasks" + (" WHERE status=?" if status else "") + " ORDER BY id"
    return db.execute(q, (status,) if status else ()).fetchall()


def ready_tasks(db):
    done = {r["id"] for r in db.execute("SELECT id FROM tasks WHERE status='done'")}
    return [t for t in list_tasks(db, "pending") if set(json.loads(t["depends_on"])) <= done]


def start(db, task_id, tier, model):
    db.execute("UPDATE tasks SET status='running',tier=?,model=?,started_at=? WHERE id=?", (tier, model, now(), task_id))
    db.commit()


def finish(db, task_id, ok, deltas=None, notes="", report="", touched=()):
    t = get(db, task_id)
    merged = sorted(set(json.loads(t["files_touched"])) | set(touched))  # across attempts: a retry may change nothing new
    failures = t["failures"] + (0 if ok else 1)
    status = "done" if ok else "pending"
    db.execute(
        "UPDATE tasks SET status=?,failures=?,notes=?,last_report=?,files_touched=?,finished_at=? WHERE id=?",
        (status, failures, notes or t["notes"], "" if ok else report, json.dumps(merged), now(), task_id),
    )
    db.execute(
        "INSERT INTO runs(task_id,complexity,kind,tier,ok,deltas,at) VALUES(?,?,?,?,?,?,?)",
        (task_id, t["complexity"], t["kind"], t["tier"], int(ok), json.dumps(deltas or {}), now()),
    )
    db.commit()


def block(db, task_id, reason):
    db.execute("UPDATE tasks SET status='blocked',notes=? WHERE id=?", (reason, task_id))
    db.commit()


def requeue_interrupted(db):
    """Tasks left 'running' by a crash go back to pending (they never reached finish)."""
    db.execute("UPDATE tasks SET status='pending' WHERE status='running'")
    db.commit()


def history(db, window: str, complexity: str) -> list[float]:
    """Measured usage delta (percentage points) for successful runs of this class."""
    out = []
    for r in db.execute("SELECT deltas FROM runs WHERE ok=1 AND complexity=?", (complexity,)):
        v = json.loads(r["deltas"]).get(window)
        if v is not None and v >= 0:
            out.append(float(v))
    return out


def success_rate(db, kind: str, complexity: str, tier: str) -> tuple[int, float]:
    """(runs, success rate) for this class at this tier, from measured history."""
    rows = db.execute("SELECT ok FROM runs WHERE kind=? AND complexity=? AND tier=?", (kind, complexity, tier)).fetchall()
    n = len(rows)
    return n, (sum(r["ok"] for r in rows) / n if n else 1.0)


def protect(db, task_id: int, paths) -> None:
    """Files a finished test task wrote: later non-test tasks may not modify them."""
    db.executemany("INSERT OR REPLACE INTO protected(path,task_id) VALUES(?,?)", [(p, task_id) for p in paths])
    db.commit()


def protected(db) -> set:
    return {r["path"] for r in db.execute("SELECT path FROM protected")}

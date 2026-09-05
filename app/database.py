import fcntl
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from app.errors import Code, Error
from app.models import Inventory, Run, utc_now


class Store:
    """One service process owns this file; SQLite persists snapshots and job history."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = None

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(".lockfile").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("Warmer state already has an owner; use one instance and one worker") from None
        self._lock = lock
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inventory (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, active INTEGER NOT NULL, body TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(active) WHERE active=1;
            """)
            for (body,) in db.execute("SELECT body FROM runs WHERE active=1").fetchall():
                run = Run.model_validate_json(body)
                run.status = "interrupted"
                run.error = "process_restarted"
                run.finished_at = utc_now()
                run.missing_pops = sorted(set(run.target_pops) - set(run.covered_pops))
                db.execute("UPDATE runs SET active=0,body=? WHERE id=?", (run.model_dump_json(), run.id))

    def close(self) -> None:
        if self._lock:
            self._lock.close()
            self._lock = None

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def healthy(self) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1").fetchone() == (1,)

    def get_inventory(self) -> Inventory | None:
        with self.connect() as db:
            row = db.execute("SELECT body FROM inventory WHERE id=1").fetchone()
        return Inventory.model_validate_json(row[0]) if row else None

    def save_inventory(self, inventory: Inventory) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO inventory VALUES (1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (inventory.model_dump_json(),))

    def start_run(self, run: Run) -> None:
        try:
            with self.connect() as db:
                db.execute("INSERT INTO runs VALUES (?,1,?)", (run.id, run.model_dump_json()))
        except sqlite3.IntegrityError:
            raise Error(Code.RUN_IN_PROGRESS) from None

    def reserve_run(self, operation: str, name: str | None, max_age: int) -> tuple[Run, Inventory | None]:
        # Inventory selection and admission share a transaction: discovery cannot
        # finish between an old target snapshot and reservation of the next job.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM runs WHERE active=1").fetchone():
                raise Error(Code.RUN_IN_PROGRESS)
            row = db.execute("SELECT body FROM inventory WHERE id=1").fetchone()
            inventory = Inventory.model_validate_json(row[0]) if row else None
            if operation == "warm" and (inventory is None or utc_now() - inventory.fetched_at > timedelta(seconds=max_age)):
                raise Error(Code.DISCOVERY_REQUIRED)
            targets = sorted(p.code for p in inventory.pops) if inventory else []
            run = Run(id=uuid4().hex, operation=operation, query_name=name,
                      target_pops=targets, missing_pops=targets)
            db.execute("INSERT INTO runs VALUES (?,1,?)", (run.id, run.model_dump_json()))
        return run, inventory

    def save_run(self, run: Run) -> None:
        with self.connect() as db:
            # A delayed checkpoint must never resurrect or overwrite a terminal run.
            db.execute("UPDATE runs SET active=?,body=? WHERE id=? AND active=1", (int(run.status == "running"), run.model_dump_json(), run.id))

    def get_run(self, run_id: str) -> Run | None:
        with self.connect() as db:
            row = db.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
        return Run.model_validate_json(row[0]) if row else None

    def latest_runs(self) -> list[Run]:
        with self.connect() as db:
            rows = db.execute("SELECT body FROM runs ORDER BY rowid DESC LIMIT 20").fetchall()
        return [Run.model_validate_json(row[0]) for row in rows]

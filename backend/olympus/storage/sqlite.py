"""One SQLite transaction commits snapshot, immutable event and operation receipt.

This is a persistence primitive, not an authorization engine or scheduler. P3 must
validate permissions, transitions and evidence before supplying the proposed state.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from olympus.contracts.models import Command, Event, Receipt, Run, fingerprint


class IdempotencyConflict(ValueError):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, version INTEGER NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(run_id TEXT NOT NULL, seq INTEGER NOT NULL, id TEXT UNIQUE NOT NULL, body TEXT NOT NULL, PRIMARY KEY(run_id,seq));
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_event_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_event_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    def create(self, run: Run, *, version: int = 1):
        if run.event_seq != 0 or run.state_revision != 0 or version < 1:
            raise ValueError("new runs start at revision/sequence zero with positive version")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute("SELECT version,body FROM runs WHERE id=?", (run.run_id,)).fetchone()
                if existing:
                    if existing != (version, run.model_dump_json()):
                        raise IdempotencyConflict("run ID already has different content")
                else:
                    db.execute(
                        "INSERT INTO runs VALUES (?,?,?)", (run.run_id, version, run.model_dump_json())
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise

    def snapshot(self, run_id: str) -> Run:
        with self.connection() as db:
            row = db.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return Run.model_validate_json(row[0])

    def events(self, run_id: str, after_seq: int = 0) -> list[Event]:
        if after_seq < 0:
            raise ValueError("negative cursor")
        with self.connection() as db:
            rows = db.execute(
                "SELECT body FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after_seq)
            ).fetchall()
        return [Event.model_validate_json(r[0]) for r in rows]

    def commit(
        self,
        command: Command,
        proposed: Run,
        *,
        reason: str,
        task_id: str | None = None,
        attempt_id: str | None = None,
        actor_type="program",
        next_version: int | None = None,
    ) -> Receipt:
        """CAS + idempotency. Changed input under same operation_id is an explicit conflict.

        Retry must reuse the same proposal. Rejected operations are persisted too.
        A future command layer must authenticate actor before calling this method.
        """
        if proposed.run_id != command.run_id:
            raise ValueError("cross-run proposal")
        # Include the whole effect, so accidental reuse cannot silently replace an earlier operation.
        signature = fingerprint(
            {
                "command": command.model_dump(mode="json"),
                "proposed": proposed.model_dump(mode="json"),
                "reason": reason,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "actor_type": actor_type,
                "next_version": next_version,
            }
        )
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                previous = db.execute(
                    "SELECT input_hash,body FROM operations WHERE id=?", (command.operation_id,)
                ).fetchone()
                if previous:
                    if previous[0] != signature:
                        raise IdempotencyConflict("operation ID reused with different input")
                    db.commit()
                    return Receipt.model_validate_json(previous[1])
                row = db.execute("SELECT version,body FROM runs WHERE id=?", (command.run_id,)).fetchone()
                if not row:
                    raise KeyError(command.run_id)
                version, raw = row
                current = Run.model_validate_json(raw)
                ok = (
                    version == command.expected_version
                    and current.state_revision == command.expected_state_revision
                )
                receipt = Receipt(
                    operation_id=command.operation_id,
                    run_id=command.run_id,
                    status="accepted" if ok else "rejected",
                    reason_code="COMMITTED" if ok else "STALE_VERSION",
                    state_revision=current.state_revision + (1 if ok else 0),
                )
                if ok:
                    if next_version is not None and next_version != version + 1:
                        raise ValueError("version must advance exactly once")
                    if (
                        proposed.state_revision != current.state_revision
                        or proposed.event_seq != current.event_seq
                    ):
                        raise ValueError("proposal must be based on current snapshot counters")
                    data = proposed.model_dump(mode="json")
                    data.update(state_revision=current.state_revision + 1, event_seq=current.event_seq + 1)
                    updated = Run.model_validate(data)
                    old_task = next((t for t in current.tasks if t.task_id == task_id), None)
                    new_task = next((t for t in updated.tasks if t.task_id == task_id), None)
                    event = Event(
                        event_id=str(uuid4()),
                        run_id=current.run_id,
                        event_seq=updated.event_seq,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        old_state=old_task.state if old_task else None,
                        new_state=new_task.state if new_task else None,
                        reason=reason,
                        actor_type=actor_type,
                        occurred_at=datetime.now(timezone.utc),
                        state_revision=updated.state_revision,
                        snapshot=updated,
                    )
                    db.execute(
                        "UPDATE runs SET version=?,body=? WHERE id=?",
                        (next_version or version, updated.model_dump_json(), current.run_id),
                    )
                    db.execute(
                        "INSERT INTO events VALUES (?,?,?,?)",
                        (current.run_id, updated.event_seq, event.event_id, event.model_dump_json()),
                    )
                db.execute(
                    "INSERT INTO operations VALUES (?,?,?)",
                    (command.operation_id, signature, receipt.model_dump_json()),
                )
                db.commit()
                return receipt
            except Exception:
                db.rollback()
                raise

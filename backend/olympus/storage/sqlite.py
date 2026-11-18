"""One SQLite transaction commits snapshot, immutable event and operation receipt.

This is a persistence primitive, not an authorization engine or scheduler. P3 must
validate permissions, transitions and evidence before supplying the proposed state.
"""

import json
import sqlite3
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from olympus.contracts.models import Command, Event, Receipt, Run, fingerprint


class IdempotencyConflict(ValueError):
    pass


class ConcurrentCommit(ValueError):
    """The persisted baseline changed before this batch could be committed."""


def command_fingerprint(command: Command) -> str:
    """Match CommandBus semantics: a revision guard is not a new business intent."""
    return fingerprint(command.model_dump(mode="json", exclude={"expected_state_revision"}))


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
                CREATE TABLE IF NOT EXISTS runtime_bindings(run_id TEXT PRIMARY KEY, body TEXT NOT NULL);
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

    def save_runtime(self, run_id: str, state: dict) -> None:
        """Save trusted host metadata; use append_events when state also changed."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone() is None:
                    raise KeyError(run_id)
                self._write_runtime(db, run_id, state)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def load_runtime(self, run_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT body FROM runtime_bindings WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _write_runtime(db, run_id: str, state: dict) -> None:
        body = json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False)
        db.execute(
            "INSERT INTO runtime_bindings(run_id,body) VALUES (?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET body=excluded.body", (run_id, body),
        )

    @staticmethod
    def _append_batch(db, run_id: str, events: Sequence[Event], expected: Run) -> Run:
        row = db.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        current = Run.model_validate_json(row[0])
        if expected.run_id != run_id or current != expected:
            raise ConcurrentCommit("persisted run changed before batch commit")
        previous = current
        for raw in events:
            # Revalidate callers' model_copy(update=...) objects as well.
            event = Event.model_validate(raw.model_dump(mode="json"))
            if event.run_id != run_id or event.event_seq != previous.event_seq + 1:
                raise ValueError("batch events must continue the committed sequence")
            if event.state_revision - previous.state_revision not in (0, 1):
                raise ValueError("batch event revisions must advance by zero or one")
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?)",
                (run_id, event.event_seq, event.event_id, event.model_dump_json()),
            )
            previous = event.snapshot
        if events:
            db.execute("UPDATE runs SET body=? WHERE id=?", (previous.model_dump_json(), run_id))
        return previous

    def append_events(
        self, run_id: str, events: Sequence[Event], *, expected: Run,
        runtime_state: dict | None = None,
    ) -> int:
        """Atomically commit a program's event batch, snapshot and runtime metadata."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._append_batch(db, run_id, events, expected)
                if runtime_state is not None:
                    self._write_runtime(db, run_id, runtime_state)
                db.commit()
                return len(events)
            except Exception:
                db.rollback()
                raise

    def command_receipt(self, command: Command) -> Receipt | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT input_hash,body FROM operations WHERE id=?", (command.operation_id,),
            ).fetchone()
        if row is None:
            return None
        if row[0] != command_fingerprint(command):
            raise IdempotencyConflict("operation ID reused with different input")
        return Receipt.model_validate_json(row[1])

    def commit_command(
        self, command: Command, receipt: Receipt, events: Sequence[Event], *, expected: Run,
        runtime_state: dict | None = None,
    ) -> tuple[Receipt, int]:
        """Commit accepted or rejected receipt and all effects in one transaction.

        Replaying an identical operation returns its original receipt without
        appending events. The full effect is produced by the command authority;
        this layer validates the event chain and compare-and-swap baseline.
        """
        if receipt.operation_id != command.operation_id or receipt.run_id != command.run_id:
            raise ValueError("receipt does not belong to command")
        if receipt.status != "accepted" and events:
            raise ValueError("rejected commands cannot commit state changes")
        signature = command_fingerprint(command)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT input_hash,body FROM operations WHERE id=?", (command.operation_id,),
                ).fetchone()
                if row is not None:
                    if row[0] != signature:
                        raise IdempotencyConflict("operation ID reused with different input")
                    db.commit()
                    return Receipt.model_validate_json(row[1]), 0
                final = self._append_batch(db, command.run_id, events, expected)
                if receipt.state_revision != final.state_revision:
                    raise ValueError("receipt revision does not match batch final snapshot")
                if runtime_state is not None:
                    self._write_runtime(db, command.run_id, runtime_state)
                db.execute(
                    "INSERT INTO operations VALUES (?,?,?)",
                    (command.operation_id, signature, receipt.model_dump_json()),
                )
                db.commit()
                return receipt, len(events)
            except Exception:
                db.rollback()
                raise

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

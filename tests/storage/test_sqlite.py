from concurrent.futures import ThreadPoolExecutor

import pytest

from olympus.contracts.models import Command, Run
from olympus.storage.sqlite import IdempotencyConflict, Store


def pause(run):
    return Run.model_validate(run.model_dump() | {"status": "paused"})


def test_atomic_commit_idempotency_restart_and_stale(tmp_path, bundle):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    store.create(bundle.run)
    store.create(bundle.run)
    proposed = pause(bundle.run)
    receipt = store.commit(bundle.command, proposed, reason="test pause")
    assert receipt.status == "accepted"
    reopened = Store(path)
    assert reopened.snapshot(bundle.run.run_id).status == "paused"
    assert reopened.commit(bundle.command, proposed, reason="test pause") == receipt
    assert len(reopened.events(bundle.run.run_id)) == 1
    assert reopened.events(bundle.run.run_id, after_seq=1) == []
    stale = Command.model_validate(bundle.command.model_dump() | {"operation_id": "OP-stale"})
    refused = reopened.commit(stale, proposed, reason="stale")
    assert refused.reason_code == "STALE_VERSION"
    assert reopened.commit(stale, proposed, reason="stale") == refused
    assert reopened.snapshot(bundle.run.run_id).state_revision == 1
    with pytest.raises(IdempotencyConflict):
        reopened.commit(bundle.command, proposed, reason="different intent")


def test_only_one_concurrent_cas_wins(tmp_path, bundle):
    store = Store(tmp_path / "state.sqlite3")
    store.create(bundle.run)

    def attempt(i):
        cmd = Command.model_validate(bundle.command.model_dump() | {"operation_id": f"OP-{i}"})
        return store.commit(cmd, pause(bundle.run), reason="concurrent pause")

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(attempt, range(5)))
    assert sum(r.status == "accepted" for r in results) == 1
    assert len(store.events(bundle.run.run_id)) == 1


def test_failure_rolls_back_snapshot_event_and_receipt(tmp_path, bundle):
    store = Store(tmp_path / "state.sqlite3")
    store.create(bundle.run)
    with store.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_receipt BEFORE INSERT ON operations BEGIN SELECT RAISE(ABORT,'injected disk write failure'); END"
        )
    with pytest.raises(Exception, match="injected"):
        store.commit(bundle.command, pause(bundle.run), reason="will rollback")
    assert store.snapshot(bundle.run.run_id).state_revision == 0
    assert store.events(bundle.run.run_id) == []
    with store.connection() as db:
        db.execute("DROP TRIGGER fail_receipt")
    assert store.commit(bundle.command, pause(bundle.run), reason="will rollback").status == "accepted"


def test_version_and_immutable_event(tmp_path, bundle):
    store = Store(tmp_path / "state.sqlite3")
    store.create(bundle.run, version=2)
    assert store.commit(bundle.command, pause(bundle.run), reason="bad version").status == "rejected"
    current = Command.model_validate(
        bundle.command.model_dump() | {"expected_version": 2, "operation_id": "OP-v2"}
    )
    store.commit(current, pause(bundle.run), reason="version advance", next_version=3)
    with store.connection() as db, pytest.raises(Exception, match="immutable"):
        db.execute("DELETE FROM events")
    assert store.events(bundle.run.run_id)[0].snapshot == store.snapshot(bundle.run.run_id)

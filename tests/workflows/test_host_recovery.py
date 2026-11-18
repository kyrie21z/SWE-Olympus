"""Host stage recovery must not invent human approval or duplicate service state."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading

from olympus.agents.runtime import AgentRunner, MockProvider
from olympus.orchestrator.recovery import RuntimeRegistry
from olympus.storage.sqlite import Store
from olympus.workflows import WorkflowService


def test_failed_approval_is_not_exposed_as_approved(tmp_path, bundle, monkeypatch):
    store = Store(tmp_path / "state.sqlite3")
    registry = RuntimeRegistry(store)
    host = WorkflowService(store, registry, lambda ledger: None)
    try:
        doc = host.create(bundle.requirement.model_dump(mode="json"), allow_synthetic=True)
        runtime = registry.get(doc["run_id"])

        def failed_write(*args, **kwargs):
            raise OSError("simulated full disk")

        monkeypatch.setattr(runtime, "persist", failed_write)
        result = host.act(doc["workflow_id"], "approve_requirement", "approval-failure")
        assert result["stage"] == "error"
        assert result["error"]["code"] == "PERSISTENCE_REQUIRES_RECONCILIATION"
        assert result["snapshot"]["approved_package"] == []
        assert result["retry_safe"] is False
        assert result["allowed_actions"] == []
    finally:
        host.close()


def test_interrupted_human_action_is_not_assumed_complete(tmp_path, bundle):
    store = Store(tmp_path / "state.sqlite3")
    host = WorkflowService(store, RuntimeRegistry(store), lambda ledger: None)
    doc = host.create(bundle.requirement.model_dump(mode="json"), allow_synthetic=True)
    doc["pending_action"] = "approve_requirement"
    with store.connection() as db:
        db.execute("UPDATE workflow_documents SET body=? WHERE id=?",
                   (json.dumps(doc), doc["workflow_id"]))
    host.close()
    recovered = WorkflowService(store, RuntimeRegistry(store), lambda ledger: None)
    try:
        result = recovered.get(doc["workflow_id"])
        assert result["stage"] == "error"
        assert result["error"]["code"] == "INTERRUPTED_REQUIRES_RECONCILIATION"
        assert result["snapshot"]["approved_package"] == []
        assert not result["allowed_actions"]
    finally:
        recovered.close()


def test_concurrent_queries_share_one_runner(tmp_path, bundle):
    store = Store(tmp_path / "state.sqlite3")
    created = []
    start = threading.Barrier(8)

    def factory(ledger):
        runner = AgentRunner(MockProvider(), ledger=ledger)
        created.append(runner)
        return runner

    host = WorkflowService(store, RuntimeRegistry(store), factory)
    try:
        doc = host.create(bundle.requirement.model_dump(mode="json"), allow_synthetic=True)

        def query(_):
            start.wait(timeout=5)
            return host._runner(doc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            runners = list(pool.map(query, range(8)))
        assert len(created) == 1
        assert all(runner is runners[0] for runner in runners)
    finally:
        host.close()

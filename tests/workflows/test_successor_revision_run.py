"""A failed bound run gets a fresh linked run; immutable evidence is never rewritten."""

from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from olympus.agents.runtime import AgentRunner
from olympus.app import create_app
from olympus.storage.sqlite import Store

from test_browser_host import action, wait_for_stage
from test_direct_plan_revision import DirectRevisionProvider, prepared


def revision_body(source, operation="successor-revision"):
    tasks = deepcopy(source["plan"]["tasks"])
    tasks[-1]["goal"] += "，在后继运行修订"
    return {"tasks": tasks, "expected_plan_ref": {key: source["plan"][key]
            for key in ("schema_version", "object_id", "version", "content_hash")}, "operation_id": operation}


def blocked_source(client, bundle):
    doc = prepared(client, bundle)
    action(client, doc["workflow_id"], "approve_plan", "plan_approved")
    # This scripted provider has no implementation fixture: the actual local
    # executor records a known failed receipt without fabricated artifacts.
    return action(client, doc["workflow_id"], "execute", "execution_blocked")


def test_successor_has_fresh_state_remaining_budget_full_review_and_preserves_source_evidence(tmp_path, bundle):
    provider = DirectRevisionProvider(bundle)
    store = Store(tmp_path / "state.sqlite3")

    def factory(ledger):
        return AgentRunner(provider, ledger=ledger)

    with TestClient(create_app(store=store, runner_factory=factory)) as client:
        source = blocked_source(client, bundle)
        assert source["snapshot"]["tasks"][0]["state"] == "failed"
        old_snapshot = store.snapshot(source["run_id"])
        old_events = store.events(source["run_id"])
        count = len(provider.requests)
        body = revision_body(source)
        response = client.post(f"/api/workflows/{source['workflow_id']}/plan-revision-runs", json=body)
        assert response.status_code == 200, response.text
        child = wait_for_stage(client, response.json()["workflow_id"], "plan_pending_approval")
        assert child["source_workflow_id"] == source["workflow_id"] and child["source_run_id"] == source["run_id"]
        assert child["workflow_id"] != source["workflow_id"] and child["run_id"] != source["run_id"]
        assert child["plan"]["version"] == source["plan"]["version"] + 1
        assert child["requirement"] == source["requirement"] and child["design"] == source["design"]
        assert child["approvals"] == {key: source["approvals"][key] for key in ("requirement", "design")}
        assert child["snapshot"]["status"] == "draft" and child["snapshot"]["tasks"] == []
        assert child["executions"] == [] and "plan" not in child["approvals"]
        assert child["call_budget"] == source["call_budget"] - source["snapshot"]["calls_used"]
        assert child["revision_budget"]["root_call_budget"] == source["call_budget"]
        assert [request.task_kind for request in provider.requests[count:]] == ["plan_review", "acceptance_definition"]
        parent = client.get(f"/api/workflows/{source['workflow_id']}").json()
        assert parent["successor_workflow_id"] == child["workflow_id"]
        for key in ("plan", "acceptance", "acceptance_plan", "approvals", "executions", "stage", "error"):
            assert parent[key] == source[key]
        assert store.snapshot(source["run_id"]) == old_snapshot and store.events(source["run_id"]) == old_events
        assert "execute" not in parent["allowed_actions"]
        replay_count = len(provider.requests)
        replay = client.post(f"/api/workflows/{source['workflow_id']}/plan-revision-runs", json=body)
        assert replay.status_code == 200 and replay.json()["workflow_id"] == child["workflow_id"]
        assert len(provider.requests) == replay_count
        second = deepcopy(body)
        second["operation_id"] = "second-child"
        assert client.post(f"/api/workflows/{source['workflow_id']}/plan-revision-runs", json=second).status_code == 409
        changed = deepcopy(body)
        changed["tasks"][-1]["goal"] += "changed"
        assert client.post(f"/api/workflows/{source['workflow_id']}/plan-revision-runs", json=changed).status_code == 409
        assert client.post(f"/api/workflows/{source['workflow_id']}/actions", json={
            "action": "execute", "operation_id": "old-source-resume",
        }).status_code == 409


@pytest.mark.parametrize("guard", ["not_failed", "stale", "pending", "unknown", "running", "no_budget"])
def test_successor_rejects_unsettled_stale_or_exhausted_sources_without_creating_run(tmp_path, bundle, guard):
    provider = DirectRevisionProvider(bundle)
    store = Store(tmp_path / "state.sqlite3")

    def factory(ledger):
        return AgentRunner(provider, ledger=ledger)

    app = create_app(store=store, runner_factory=factory)
    with TestClient(app) as client:
        source = prepared(client, bundle) if guard == "not_failed" else blocked_source(client, bundle)
        body = revision_body(source)
        if guard == "stale":
            body["expected_plan_ref"]["version"] += 1
        elif guard in {"pending", "no_budget"}:
            host = app.state.workflows
            doc = host._read(source["workflow_id"])
            if guard == "pending":
                doc["pending_action"] = "execute"
            else:
                doc["call_budget"] = host._ledger(source["run_id"]).total_calls_accounted(source["run_id"])
            host._save(doc)
        elif guard == "unknown":
            ledger = app.state.workflows._ledger(source["run_id"])
            ledger.record_outcome(ledger.entries()[0].request_id, "outcome_unknown", error_code="provider_timeout")
        elif guard == "running":
            runtime = app.state.workflows.registry.get(source["run_id"])
            runtime.ctx.run.tasks[0].state = "running"
        count = len(provider.requests)
        with store.connection() as db:
            run_count = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        response = client.post(f"/api/workflows/{source['workflow_id']}/plan-revision-runs", json=body)
        assert response.status_code == 409, response.text
        assert len(provider.requests) == count
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == run_count
            assert db.execute("SELECT 1 FROM workflow_operations WHERE id=?", (body["operation_id"],)).fetchone() is None

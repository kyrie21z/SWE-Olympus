"""Exercise the no-credentials default through the real application and Python tools."""

import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from olympus.app import create_app
from olympus.contracts.models import Requirement
from olympus.storage.sqlite import Store

from test_browser_host import action, create, wait_for_stage

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(("fixture", "task_count"), [
    ("fixtures/requirements/inventory-reservation/requirement.json", 4),
    ("frontend/public/examples/requirement.json", 2),
])
def test_default_offline_runs_builtin_requirement_to_acceptance(tmp_path, fixture, task_count):
    req = Requirement.model_validate_json((ROOT / fixture).read_text(encoding="utf-8"))
    with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"))) as client:
        initial = create(client, req)
        assert initial["model_mode"] == "offline"
        workflow_id = initial["workflow_id"]
        action(client, workflow_id, "approve_requirement", "requirement_approved")
        designed = action(client, workflow_id, "generate_design", "design_pending_approval")
        assert designed["design_review"]["review"]["mode"] == "mock"
        action(client, workflow_id, "approve_design", "design_approved")
        action(client, workflow_id, "generate_plan", "plan_pending_approval")
        action(client, workflow_id, "approve_plan", "plan_approved")
        action(client, workflow_id, "execute", "awaiting_acceptance")
        final = action(client, workflow_id, "accept_run", "completed")
        assert final["snapshot"]["mode"] == "real_tool"
        assert len(final["executions"]) == task_count
        assert all(item["runtime"]["state"] == "accepted" for item in final["executions"])
        assert all(item["execution"]["tool_invocations"] > 0 for item in final["executions"])
        if task_count == 4:
            outputs = final["executions"][-1]["execution"]["check_outputs"]
            assert "Ran 7 tests" in outputs[0]["output"]
        view = client.get(f"/api/runs/{initial['run_id']}/view").json()
        assert view["projection"]["progress"]["accepted"] == task_count
        assert all(row["accepted"] for row in view["trace"]["rows"])


def test_preexisting_missing_configuration_error_can_retry_offline(tmp_path):
    req = Requirement.model_validate_json(
        (ROOT / "fixtures/requirements/inventory-reservation/requirement.json").read_text(encoding="utf-8")
    )
    store = Store(tmp_path / "state.sqlite3")
    with TestClient(create_app(store=store)) as client:
        initial = create(client, req)
        workflow_id = initial["workflow_id"]
        approved = action(client, workflow_id, "approve_requirement", "requirement_approved")
        with store.connection() as db:
            approved.pop("model_mode")
            approved.update(stage="error", error={"code": "AGENT_NOT_CONFIGURED", "message": "old error"},
                            retry_action="generate_design", retry_safe=True)
            db.execute("UPDATE workflow_documents SET body=? WHERE id=?", (json.dumps(approved), workflow_id))
        reply = client.post(f"/api/workflows/{workflow_id}/actions", json={
            "action": "generate_design", "operation_id": "retry-with-offline-default",
        })
        assert reply.status_code == 200
        result = wait_for_stage(client, workflow_id, "design_pending_approval")
        assert result["model_mode"] == "offline"


def test_unmatched_requirement_does_not_get_unrelated_offline_output(tmp_path, bundle):
    with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"))) as client:
        req = bundle.requirement.model_copy(update={"object_id": "REQ-unmatched", "confirmation": None})
        workflow_id = create(client, req)["workflow_id"]
        action(client, workflow_id, "approve_requirement", "requirement_approved")
        failed = action(client, workflow_id, "generate_design", "error")
        assert failed["error"]["code"] == "OFFLINE_REQUIREMENT_UNSUPPORTED"
        assert failed["design"] is None
        assert failed["snapshot"]["calls_used"] == 0

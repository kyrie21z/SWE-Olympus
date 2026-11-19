"""Three isolated offline-preset API rounds with real Python execution.

These tests validate workflow integration and the shipped deterministic agents;
they do not claim to validate online model quality or contact a model endpoint.
"""

from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from olympus.app import create_app
from olympus.contracts.models import Requirement
from olympus.storage.sqlite import Store

from test_browser_host import action, wait_for_stage


ROOT = Path(__file__).resolve().parents[2]
REQUIREMENT = ROOT / "frontend/public/examples/team-documents-demo.json"
BUSINESS_PROBE = '''import json,sys
sys.path.insert(0, sys.argv[1])
from documents.store import DocumentStore
from documents.search import search_titles
from documents.history import read_version
store = DocumentStore()
store.save('交接指南', '初稿')
store.save('交接指南', '修订')
observed = {
    'search': search_titles(store.titles(), '指南'),
    'missing': search_titles(store.titles(), '无匹配'),
    'version_1': read_version(store.versions('交接指南'), 1),
    'version_2': read_version(store.versions('交接指南'), 2),
}
assert observed == {
    'search': ['交接指南'], 'missing': [], 'version_1': '初稿', 'version_2': '修订',
}
print(json.dumps(observed))
'''


def replay_without_new_calls(client, workflow_id, action_name, expected_stage, operation_id, before):
    replayed = action(client, workflow_id, action_name, expected_stage, operation_id=operation_id)
    assert replayed["snapshot"]["calls_used"] == before["snapshot"]["calls_used"]
    assert replayed["acceptance"] == before["acceptance"]
    assert replayed["executions"] == before["executions"]
    return replayed


@pytest.mark.parametrize("scenario", ["normal", "restart_after_freeze", "operation_replay"])
def test_team_documents_demo_three_offline_api_rounds_with_real_python(tmp_path, scenario):
    requirement = Requirement.model_validate_json(REQUIREMENT.read_text(encoding="utf-8"))
    assert requirement.version == 2
    assert requirement.content_hash == "c3c4aa8e4d2493bbafb39e8d14ebd5775997ad4bc20812801d571655e1f75ff9"
    database = tmp_path / "state.sqlite3"
    with ExitStack() as stack:
        client = stack.enter_context(TestClient(create_app(store=Store(database))))
        created = client.post("/api/workflows", json={
            "requirement": requirement.model_dump(mode="json"), "allow_synthetic": True, "model_mode": "offline",
        })
        assert created.status_code == 200, created.text
        initial = created.json()
        workflow_id, run_id = initial["workflow_id"], initial["run_id"]
        assert initial["stage"] == "requirement_pending"
        assert initial["model_mode"] == "offline" and initial["offline_supported"] is True
        action(client, workflow_id, "approve_requirement", "requirement_approved")
        designed = action(client, workflow_id, "generate_design", "design_pending_approval")
        assert designed["design_review"]["review"]["mode"] == "mock"
        assert designed["design"]["requirement"]["version"] == 2
        action(client, workflow_id, "approve_design", "design_approved")
        plan_operation = f"{scenario}-generate-plan"
        planned = action(client, workflow_id, "generate_plan", "plan_pending_approval", operation_id=plan_operation)
        assert len(planned["plan"]["tasks"]) == len(planned["acceptance"]) == 5
        assert planned["plan_review_record"]["review"]["mode"] == "mock"
        assert planned["plan_review_record"]["review"]["verdict"] == "passed"

        if scenario == "operation_replay":
            replay_without_new_calls(client, workflow_id, "generate_plan", "plan_pending_approval",
                                     plan_operation, planned)
        if scenario == "restart_after_freeze":
            stack.close()  # The first app has no in-flight task at this human approval gate.
            client = stack.enter_context(TestClient(create_app(store=Store(database))))
            restored = wait_for_stage(client, workflow_id, "plan_pending_approval")
            assert restored["acceptance"] == planned["acceptance"]
            assert restored["acceptance_plan"] == planned["acceptance_plan"]
            assert restored["plan"] == planned["plan"]
            assert restored["snapshot"]["calls_used"] == planned["snapshot"]["calls_used"]

        approved = action(client, workflow_id, "approve_plan", "plan_approved")
        assert approved["plan_bound"] is True
        execute_operation = f"{scenario}-execute"
        executed = action(client, workflow_id, "execute", "awaiting_acceptance", operation_id=execute_operation)
        if scenario == "operation_replay":
            replay_without_new_calls(client, workflow_id, "execute", "awaiting_acceptance", execute_operation, executed)
        final = action(client, workflow_id, "accept_run", "completed")
        assert final["model_mode"] == "offline" and final["snapshot"]["mode"] == "real_tool"
        assert final["snapshot"]["status"] == "completed"
        assert len(final["executions"]) == 5
        by_task = {item["task_id"]: item for item in final["executions"]}
        for item in by_task.values():
            receipt = item["execution"]
            assert item["runtime"]["state"] == "accepted"
            assert receipt["tool_mode"] == "real_tool" and receipt["model_mode"] in {"mock", "not_called"}
            assert receipt["tool_invocations"] == len(receipt["check_outputs"]) == 1
            check = receipt["check_outputs"][0]
            assert check["exit_code"] == 0, check["output"]
            assert Path(check["command"][0]).resolve() == Path(sys.executable).resolve()
            assert check["command"][1:4] == ["-I", "-S", "-B"]
            assert all(Path(check["command"][index]).is_file() for index in (4, 6, 7))
        assert "Title search and both document versions passed." in by_task["T04"]["execution"]["check_outputs"][0]["output"]
        assert "Delivery evidence verified" in by_task["T05"]["execution"]["check_outputs"][0]["output"]
        view = client.get(f"/api/runs/{run_id}/view")
        assert view.status_code == 200, view.text
        assert view.json()["projection"]["progress"] == {"accepted": 5, "required": 5}
        assert all(row["accepted"] for row in view.json()["trace"]["rows"])

        work = by_task["T05"]["execution"]["check_outputs"][0]["command"][5]
        probe = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", BUSINESS_PROBE, work],
                               capture_output=True, text=True, timeout=10)
        assert probe.returncode == 0, probe.stdout + probe.stderr
        observed = json.loads(probe.stdout)
        assert observed == {"search": ["交接指南"], "missing": [], "version_1": "初稿", "version_2": "修订"}
        if scenario == "operation_replay":
            replay_without_new_calls(client, workflow_id, "execute", "completed", execute_operation, final)
        print(f"offline-preset round={scenario}: completed, tasks=5, real_checks=5, observed={observed}")

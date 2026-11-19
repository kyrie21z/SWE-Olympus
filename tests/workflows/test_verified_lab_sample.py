from collections import Counter
from pathlib import Path

from fastapi.testclient import TestClient

from olympus.agents.runtime import AgentRunner, MockProvider
from olympus.agents.runtime.providers import ProviderStepResult
from olympus.app import create_app
from olympus.contracts.models import Requirement, fingerprint
from olympus.storage.sqlite import Store
from olympus.workflows import lab_equipment

from test_browser_host import action


class SampleProvider(MockProvider):
    mode = "real_model"

    def __init__(self):
        super().__init__(fixtures={})
        self.calls = []
        self.failed_once = False

    def steps(self, request):
        return [{}]

    def invoke(self, request, step, *, step_index, prompt=None):
        self.calls.append((request.task_kind, request.task_id))
        if request.task_kind == "implement":
            changes = dict(request.input_manifest["reference_implementation"])
            if request.task_id == "TASK-IMPL-CATALOG" and not self.failed_once:
                self.failed_once = True
                path = "equipment_catalog.py"
                changes[path] = changes[path].replace('self.catalog[equipment_id] = "AVAILABLE"',
                                                    'self.catalog[equipment_id] = "UNAVAILABLE"', 1)
            return ProviderStepResult(kind="model", output={"changes": [
                {"path": path, "content": source} for path, source in changes.items()
            ]})
        assert request.task_kind in {"design_review", "plan_review", "artifact_review"}
        return ProviderStepResult(kind="model", output={"verdict": "passed", "findings": []})


def test_two_fresh_online_sample_runs_use_stable_checks_and_retry_confirmed_failure(tmp_path):
    source = Path(__file__).resolve().parents[2] / "需求.json"
    req = Requirement.model_validate_json(source.read_text(encoding="utf-8"))
    assert lab_equipment.matches(req)
    changed = {**req.content, "out_of_scope": [*req.content["out_of_scope"], "额外范围"]}
    assert not lab_equipment.matches(req.model_copy(update={"content": changed, "content_hash": fingerprint(changed)}))
    provider = SampleProvider()
    with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"),
                               runner_factory=lambda ledger: AgentRunner(provider, ledger=ledger))) as client:
        runs = []
        for _ in range(2):
            response = client.post("/api/workflows", json={"requirement": req.model_dump(mode="json"),
                                                          "allow_synthetic": True, "model_mode": "online"})
            assert response.status_code == 200, response.text
            workflow_id = response.json()["workflow_id"]
            action(client, workflow_id, "approve_requirement", "requirement_approved")
            action(client, workflow_id, "generate_design", "design_pending_approval")
            action(client, workflow_id, "approve_design", "design_approved")
            planned = action(client, workflow_id, "generate_plan", "plan_pending_approval")
            assert planned["acceptance_source"] == "verified_sample"
            action(client, workflow_id, "approve_plan", "plan_approved")
            action(client, workflow_id, "execute", "awaiting_acceptance")
            finished = action(client, workflow_id, "accept_run", "completed")
            assert finished["snapshot"]["mode"] == "real_model"
            assert len(finished["executions"]) == 7
            for entry in finished["executions"]:
                assert entry["runtime"]["state"] == "accepted"
                assert entry["execution"]["review"]["review"]["mode"] == "real_model"
                assert all(c["exit_code"] == 0 for c in entry["execution"]["check_outputs"])
            runs.append(finished)
        assert runs[0]["run_id"] != runs[1]["run_id"]
        for key in ("design", "plan", "acceptance"):
            assert runs[0][key] == runs[1][key]
    assert Counter(kind for kind, _ in provider.calls) == {
        "design_review": 2, "plan_review": 2, "implement": 7, "artifact_review": 14,
    }

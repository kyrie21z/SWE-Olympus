"""Bound Momus response size and retain completed checks across recoverable errors."""

from fastapi.testclient import TestClient
import pytest

from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError
from olympus.app import create_app
from olympus.storage.sqlite import Store
from olympus.workflows.acceptance import generate_acceptance

from test_browser_host import action, create, wait_for_stage


class TaskAcceptanceProvider(MockProvider):
    def __init__(self, bundle, *, fail_task=None, unknown=False, failure_code=None):
        review = {"output": {"verdict": "passed", "findings": []}}
        super().__init__(fixtures={
            "Athena:design": {"output": {"design": bundle.design.content}},
            "Momus:design_review": review,
            "Daedalus:plan": {"output": {"tasks": [task.semantic_content() for task in bundle.plan.tasks]}},
            "Momus:plan_review": review,
        })
        self.requests = []
        self.fail_task, self.unknown = fail_task, unknown
        self.failure_code = failure_code

    def steps(self, request):
        self.requests.append(request)
        if request.task_kind != "acceptance_definition":
            return super().steps(request)
        if request.task_id == self.fail_task:
            self.fail_task = None
            if self.unknown:
                return [{"status": "outcome_unknown", "error_code": "provider_timeout"}]
            if self.failure_code:
                return [{"status": "failed", "error_code": self.failure_code}]
            return [{"output": {"tasks": []}}]
        task = request.input_manifest["tasks"][0]
        return [{"output": {"tasks": [{
            "task_id": task["task_id"],
            "checks": {check: "import math\nassert math.sqrt(4) == 2\n" for check in task["checks"]},
        }]}}]


def test_each_acceptance_call_contains_one_task_and_its_available_inputs(bundle):
    provider = TaskAcceptanceProvider(bundle)
    acceptance = generate_acceptance(AgentRunner(provider), bundle.requirement, bundle.design, bundle.plan)
    assert len(provider.requests) == len(bundle.plan.tasks)
    assert len({request.idempotency_key for request in provider.requests}) == len(bundle.plan.tasks)
    for request, task in zip(provider.requests, bundle.plan.tasks, strict=True):
        assert request.task_id == task.task_id
        assert request.input_manifest["tasks"] == [task.model_dump(mode="json")]
        inputs = request.input_manifest["available_inputs"]
        assert all(row["task_id"] != task.task_id for row in inputs)
        producers = {output: row for row in bundle.plan.tasks for output in row.outputs}
        assert {producers[item].task_id for item in task.inputs if item in producers} <= {
            row["task_id"] for row in inputs
        }
        schema = request.output_schema["properties"]["tasks"]
        assert schema["minItems"] == schema["maxItems"] == 1
        FrozenAcceptance.model_validate(acceptance[task.task_id]).verify(task)


def test_partial_acceptance_resumes_without_resending_completed_tasks(bundle):
    provider = TaskAcceptanceProvider(bundle, fail_task="T02")
    runner = AgentRunner(provider)
    completed = {}
    with pytest.raises(OutputSchemaError):
        generate_acceptance(runner, bundle.requirement, bundle.design, bundle.plan,
                            checkpoint=lambda task_id, frozen: completed.update({task_id: frozen}))
    assert set(completed) == {"T01"}
    first = completed["T01"]
    # A host retry receives a fresh operation identity; completed tasks never reach the provider.
    resumed_provider = TaskAcceptanceProvider(bundle)
    result = generate_acceptance(AgentRunner(resumed_provider), bundle.requirement, bundle.design, bundle.plan,
                                 completed=completed)
    assert result["T01"] == first
    assert [request.task_id for request in resumed_provider.requests] == [
        task.task_id for task in bundle.plan.tasks if task.task_id != "T01"
    ]


@pytest.mark.parametrize(("unknown", "failure_code"), [
    (False, None), (False, "provider_output_incomplete"),
    (False, "invalid_provider_response"), (True, None),
])
def test_workflow_checkpoints_partial_checks_and_only_retries_known_failures(tmp_path, bundle, unknown, failure_code):
    database = tmp_path / "state.sqlite3"
    provider = TaskAcceptanceProvider(bundle, fail_task="T02", unknown=unknown, failure_code=failure_code)

    def factory(ledger):
        return AgentRunner(provider, ledger=ledger)
    with TestClient(create_app(store=Store(database), runner_factory=factory)) as client:
        initial = create(client, bundle.requirement)
        assert initial["call_budget"] == initial["snapshot"]["call_budget"] == 120
        workflow_id = initial["workflow_id"]
        action(client, workflow_id, "approve_requirement", "requirement_approved")
        action(client, workflow_id, "generate_design", "design_pending_approval")
        action(client, workflow_id, "approve_design", "design_approved")
        failed = action(client, workflow_id, "generate_plan", "error")
        assert set(failed["acceptance"]) == {"T01"}
        assert failed["acceptance_plan"] == {
            key: failed["plan"][key] for key in ("schema_version", "object_id", "version", "content_hash")
        }
        assert failed["plan_review"]["verdict"] == "passed"
        assert failed["retry_safe"] is (not unknown)
        assert failed["progress"] is None
        plan, first = failed["plan"], failed["acceptance"]["T01"]
        if unknown:
            assert failed["error"]["code"] == "OUTCOME_UNKNOWN"
            assert failed["error"]["role_id"] == "Momus"
            assert failed["error"]["task_kind"] == "acceptance_definition"
            assert "响应超时" in failed["error"]["message"]
            denied = client.post(f"/api/workflows/{workflow_id}/actions", json={
                "action": "generate_plan", "operation_id": "unsafe-acceptance-retry",
            })
            assert denied.status_code == 409
            return

    before = len(provider.requests)
    with TestClient(create_app(store=Store(database), runner_factory=factory)) as restarted:
        restored = restarted.get(f"/api/workflows/{workflow_id}").json()
        assert restored["acceptance"]["T01"] == first
        reply = restarted.post(f"/api/workflows/{workflow_id}/actions", json={
            "action": "generate_plan", "operation_id": "resume-acceptance",
        })
        assert reply.status_code == 200
        completed = wait_for_stage(restarted, workflow_id, "plan_pending_approval")
        assert completed["plan"] == plan
        assert completed["acceptance"]["T01"] == first
        assert len(completed["acceptance"]) == len(bundle.plan.tasks)
        resumed = provider.requests[before:]
        assert all(request.task_kind == "acceptance_definition" for request in resumed)
        assert [request.task_id for request in resumed] == [
            task.task_id for task in bundle.plan.tasks if task.task_id != "T01"
        ]

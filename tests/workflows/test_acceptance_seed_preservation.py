"""A blocked review without new checks must not erase the previous frozen seed."""

from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.agents.runtime import AgentRunner
from olympus.app import create_app
from olympus.storage.sqlite import Store
from olympus.workflows.service import WorkflowError, WorkflowService

from test_acceptance_corrections import task_with
from test_browser_host import action, wait_for_stage
from test_direct_plan_revision import DirectRevisionProvider, prepared


def twenty_one_tasks(bundle):
    tasks = list(bundle.plan.tasks)
    previous = "T06"
    for identifier, kind, executor in (("SUMMARY", "test", "Momus"), ("INTEGRATE", "integrate", "Hermes"),
                                        ("README", "implement", "Hephaestus"), ("DOC", "doc", "Hermes")):
        tasks.append(task_with(bundle.plan.tasks[0], task_id=identifier, type=kind, executor=executor,
                               depends_on=[previous], inputs=[f"artifact:{previous}"], outputs=[f"artifact:{identifier}"],
                               checks=[f"check:{identifier}"], write_scope=[f"packages/{identifier.lower()}"]))
        previous = identifier
    for number in range(9):
        identifier = f"EXTRA-{number}"
        tasks.append(task_with(bundle.plan.tasks[0], task_id=identifier, depends_on=[], inputs=[],
                               outputs=[f"artifact:{identifier}"], checks=[f"check:{identifier}"],
                               write_scope=[f"packages/extra{number}"]))
    return tasks


def test_zero_check_blocked_child_keeps_original_seed_then_reuses_seventeen_and_regenerates_four(tmp_path, bundle):
    provider = DirectRevisionProvider(bundle)
    provider.fixtures["Daedalus:plan"] = {"output": {"tasks": [task.semantic_content() for task in twenty_one_tasks(bundle)]}}

    def factory(ledger):
        return AgentRunner(provider, ledger=ledger)

    with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"), runner_factory=factory)) as client:
        source = prepared(client, bundle)
        assert len(source["acceptance"]) == 21
        source_id = source["workflow_id"]
        action(client, source_id, "approve_plan", "plan_approved")
        source = action(client, source_id, "execute", "execution_blocked")
        provider.next_review = {"output": {"verdict": "blocked", "findings": [{
            "code": "SUMMARY_FORMAT", "severity": "blocker", "task_id": "SUMMARY", "message": "Use actual observed report data.",
        }]}}
        response = client.post(f"/api/workflows/{source_id}/plan-revision-runs", json={
            "tasks": source["plan"]["tasks"], "expected_plan_ref": source["acceptance_plan"], "operation_id": "blocked-child",
        })
        assert response.status_code == 200, response.text
        child = wait_for_stage(client, response.json()["workflow_id"], "plan_review_blocked")
        assert child["acceptance"] == {} and child["acceptance_plan"] is None
        tasks = deepcopy(child["plan"]["tasks"])
        for task in tasks:
            if task["task_id"] == "SUMMARY":
                task["checks"] = ["Verify actual report counts by iteration"]
        count = len(provider.requests)
        response = client.post(f"/api/workflows/{child['workflow_id']}/plan-revisions", json={
            "tasks": tasks, "expected_plan_ref": {key: child["plan"][key]
                        for key in ("schema_version", "object_id", "version", "content_hash")},
            "operation_id": "fix-summary-only",
        })
        assert response.status_code == 200, response.text
        revised = wait_for_stage(client, child["workflow_id"], "plan_pending_approval")
        pending = revised["pending_plan_revision"]
        assert pending["baseline_plan"]["version"] == child["plan"]["version"]
        assert pending["checkpoint_plan"]["version"] == source["plan"]["version"]
        assert pending["checkpoint_acceptance"] == source["acceptance"]
        assert len(revised["acceptance_migration"]["reused_tasks"]) == 17
        assert revised["acceptance_migration"]["from_plan"] == source["acceptance_plan"]
        assert revised["acceptance_migration"]["revision_baseline"]["version"] == child["plan"]["version"]
        new_checks = [request.task_id for request in provider.requests[count:] if request.task_kind == "acceptance_definition"]
        assert new_checks == ["SUMMARY", "INTEGRATE", "README", "DOC"]
        assert sum(request.task_kind == "plan_review" for request in provider.requests[count:]) == 1
        assert any(row.get("kind") == "preserved_checkpoint_seed" and row["acceptance"] == source["acceptance"]
                   for row in revised["acceptance_history"])


@pytest.mark.parametrize("legacy", [False, True])
def test_selector_uses_pending_seed_when_current_blocked_plan_has_no_checks(bundle, legacy):
    old = bundle.plan
    frozen = {old.tasks[0].task_id: FrozenAcceptance.freeze(old.tasks[0], {
        check: "assert 1 + 1 == 2\n" for check in old.tasks[0].checks
    }).model_dump(mode="json")}
    pending = ({"baseline_plan": old.model_dump(mode="json"), "baseline_acceptance_plan": old.ref().model_dump(mode="json"),
                "baseline_acceptance": frozen} if legacy else {
        "checkpoint_plan": old.model_dump(mode="json"), "checkpoint_binding": old.ref().model_dump(mode="json"),
        "checkpoint_acceptance": frozen,
    })
    current = old.model_copy(update={"version": old.version + 1})
    doc = {"plan": current.model_dump(mode="json"), "acceptance": {}, "acceptance_plan": None,
           "pending_plan_revision": pending}
    selected = WorkflowService._select_acceptance_checkpoint(doc, current)
    assert selected["checkpoint_plan"] == old.model_dump(mode="json")
    assert selected["checkpoint_acceptance"] == frozen
    bad = deepcopy(doc)
    binding_key = "baseline_acceptance_plan" if legacy else "checkpoint_binding"
    bad["pending_plan_revision"][binding_key] = {}
    assert WorkflowService._select_acceptance_checkpoint(bad, current)["checkpoint_acceptance"] == {}
    bad = deepcopy(doc)
    checks_key = "baseline_acceptance" if legacy else "checkpoint_acceptance"
    bad["pending_plan_revision"][checks_key][old.tasks[0].task_id]["content_hash"] = "0" * 64
    with pytest.raises(WorkflowError):
        WorkflowService._select_acceptance_checkpoint(bad, current)

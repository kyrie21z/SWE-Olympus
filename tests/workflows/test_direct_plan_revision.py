"""Submit exact user-edited tasks for a new full review without replanning."""

from copy import deepcopy

from fastapi.testclient import TestClient

from olympus.agents.runtime import AgentRunner
from olympus.app import create_app
from olympus.storage.sqlite import Store

from test_acceptance_generation import TaskAcceptanceProvider
from test_browser_host import action, create, wait_for_stage


class DirectRevisionProvider(TaskAcceptanceProvider):
    def __init__(self, bundle):
        super().__init__(bundle)
        self.next_review = None

    def steps(self, request):
        if request.task_kind == "plan_review" and self.next_review:
            self.requests.append(request)
            response, self.next_review = self.next_review, None
            return [response]
        return super().steps(request)


def prepared(client, bundle):
    workflow_id = create(client, bundle.requirement)["workflow_id"]
    action(client, workflow_id, "approve_requirement", "requirement_approved")
    action(client, workflow_id, "generate_design", "design_pending_approval")
    action(client, workflow_id, "approve_design", "design_approved")
    return action(client, workflow_id, "generate_plan", "plan_pending_approval")


def body_for(doc, operation="manual-edit"):
    tasks = deepcopy(doc["plan"]["tasks"])
    tasks[0]["goal"] += "，按用户修改"
    return {"tasks": tasks, "expected_plan_ref": {key: doc["plan"][key]
            for key in ("schema_version", "object_id", "version", "content_hash")}, "operation_id": operation}


def test_exact_revision_versions_only_changed_tasks_full_reviews_and_never_replans_or_autoapproves(tmp_path, bundle):
    provider = DirectRevisionProvider(bundle)
    store = Store(tmp_path / "state.sqlite3")

    def factory(ledger):
        return AgentRunner(provider, ledger=ledger)

    with TestClient(create_app(store=store, runner_factory=factory)) as client:
        original = prepared(client, bundle)
        workflow_id = original["workflow_id"]
        body = body_for(original)
        before = len(provider.requests)
        response = client.post(f"/api/workflows/{workflow_id}/plan-revisions", json=body)
        assert response.status_code == 200, response.text
        revised = wait_for_stage(client, workflow_id, "plan_pending_approval")
        assert revised["plan"]["version"] == original["plan"]["version"] + 1
        assert revised["plan"]["object_id"] == original["plan"]["object_id"]
        assert revised["plan"]["tasks"][0]["version"] == original["plan"]["tasks"][0]["version"] + 1
        assert revised["plan"]["tasks"][0]["goal"] == body["tasks"][0]["goal"]
        assert revised["plan"]["tasks"][1:] == original["plan"]["tasks"][1:]
        assert any(item["kind"] == "human" and item["source_id"] == "YOU" for item in revised["plan"]["provenance"])
        requests = provider.requests[before:]
        assert all(request.role_id == "Momus" for request in requests)
        assert sum(request.task_kind == "plan_review" for request in requests) == 1
        assert all(request.task_kind != "plan_finding_check" for request in requests)
        assert len(revised["plan_review_history"]) >= 2
        assert revised["snapshot"]["tasks"] == [] and "plan" not in revised["approvals"]
        count = len(provider.requests)
        replay = client.post(f"/api/workflows/{workflow_id}/plan-revisions", json=body)
        assert replay.status_code == 200 and replay.json()["plan"] == revised["plan"]
        assert len(provider.requests) == count
        changed = deepcopy(body)
        changed["tasks"][0]["goal"] += "再次修改"
        assert client.post(f"/api/workflows/{workflow_id}/plan-revisions", json=changed).status_code == 409
        stale = deepcopy(body)
        stale["operation_id"] = "stale-manual-edit"
        assert client.post(f"/api/workflows/{workflow_id}/plan-revisions", json=stale).status_code == 409
        action(client, workflow_id, "approve_plan", "plan_approved")
        forbidden = body_for(revised, "approved-manual-edit")
        assert client.post(f"/api/workflows/{workflow_id}/plan-revisions", json=forbidden).status_code == 409
        assert len(provider.requests) == count


def test_invalid_revision_is_rejected_before_reservation_or_any_model_call(tmp_path, bundle):
    provider = DirectRevisionProvider(bundle)
    store = Store(tmp_path / "state.sqlite3")

    def factory(ledger):
        return AgentRunner(provider, ledger=ledger)

    with TestClient(create_app(store=store, runner_factory=factory)) as client:
        original = prepared(client, bundle)
        body = body_for(original, "invalid-manual-edit")
        body["tasks"][0]["write_scope"] = ["contract.pyi"]
        count = len(provider.requests)
        response = client.post(f"/api/workflows/{original['workflow_id']}/plan-revisions", json=body)
        assert response.status_code == 409
        assert len(provider.requests) == count
        with store.connection() as db:
            assert db.execute("SELECT 1 FROM workflow_operations WHERE id=?", (body["operation_id"],)).fetchone() is None
        assert client.get(f"/api/workflows/{original['workflow_id']}").json()["plan"] == original["plan"]


def test_unknown_manual_review_is_preserved_and_cannot_be_resent_as_another_revision(tmp_path, bundle):
    provider = DirectRevisionProvider(bundle)

    def factory(ledger):
        return AgentRunner(provider, ledger=ledger)

    with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"), runner_factory=factory)) as client:
        original = prepared(client, bundle)
        workflow_id = original["workflow_id"]
        provider.next_review = {"status": "outcome_unknown", "error_code": "provider_timeout"}
        response = client.post(f"/api/workflows/{workflow_id}/plan-revisions", json=body_for(original))
        assert response.status_code == 200
        failed = wait_for_stage(client, workflow_id, "error")
        assert failed["error"]["code"] == "OUTCOME_UNKNOWN"
        assert failed["pending_plan_revision"]["review_complete"] is False
        count = len(provider.requests)
        response = client.post(f"/api/workflows/{workflow_id}/plan-revisions", json=body_for(failed, "unknown-resubmit"))
        assert response.status_code == 409
        assert len(provider.requests) == count


def test_direct_revision_keeps_blocking_review_without_regenerating_or_freezing_checks(tmp_path, bundle):
    provider = DirectRevisionProvider(bundle)

    def factory(ledger):
        return AgentRunner(provider, ledger=ledger)

    with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"), runner_factory=factory)) as client:
        original = prepared(client, bundle)
        workflow_id = original["workflow_id"]
        provider.next_review = {"output": {"verdict": "blocked", "findings": [{
            "code": "MISSING_CASE", "severity": "blocker", "task_id": "T01", "message": "Add the missing failure case.",
        }]}}
        count = len(provider.requests)
        response = client.post(f"/api/workflows/{workflow_id}/plan-revisions", json=body_for(original))
        assert response.status_code == 200
        blocked = wait_for_stage(client, workflow_id, "plan_review_blocked")
        assert blocked["plan_review"]["verdict"] == "blocked"
        assert [request.task_kind for request in provider.requests[count:]] == ["plan_review"]
        assert "plan" not in blocked["approvals"]


def test_known_acceptance_failure_reuses_direct_revision_and_fresh_review_on_retry(tmp_path, bundle):
    provider = DirectRevisionProvider(bundle)

    def factory(ledger):
        return AgentRunner(provider, ledger=ledger)

    with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"), runner_factory=factory)) as client:
        original = prepared(client, bundle)
        workflow_id = original["workflow_id"]
        provider.fail_task = "T02"
        response = client.post(f"/api/workflows/{workflow_id}/plan-revisions", json=body_for(original))
        assert response.status_code == 200
        failed = wait_for_stage(client, workflow_id, "error")
        assert failed["retry_safe"] is True and failed["retry_action"] == "generate_plan_revision"
        assert failed["pending_plan_revision"]["review_complete"] is True
        assert set(failed["acceptance"]) == {"T01"}
        count = len(provider.requests)
        response = client.post(f"/api/workflows/{workflow_id}/actions", json={
            "action": "generate_plan_revision", "operation_id": "retry-direct-checks",
        })
        assert response.status_code == 200
        resumed = wait_for_stage(client, workflow_id, "plan_pending_approval")
        assert resumed["plan"] == failed["plan"]
        assert all(request.task_kind == "acceptance_definition" for request in provider.requests[count:])
        assert "T01" not in [request.task_id for request in provider.requests[count:]]

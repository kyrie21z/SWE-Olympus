"""Regression cases for P1/P2 handoff; all providers are local test doubles."""

import json
from pathlib import Path

import pytest

from olympus.agents.daedalus import DaedalusPlanner
from olympus.agents.momus.planning import MomusPlanReviewer, PlanningWorkflow
from olympus.agents.runtime import (
    AgentRunner,
    InputWhitelistError,
    MockProvider,
    OutputSchemaError,
    RealProvider,
    RunnerPermissionError,
)
from olympus.contracts.models import Design, Plan, Ref, Requirement, Task, fingerprint
from olympus.planning.compiler import PlanCompilationError, compile_plan
from olympus.planning.service import PlanRejectedError, PlanService
from olympus.planning.validator import validate_plan_semantics
from olympus.requests import RequestLedger
from olympus.traceability.planning import build_planning_traceability


@pytest.fixture
def documents(bundle):
    path = Path(__file__).resolve().parents[2] / "frontend/public/examples/requirement.json"
    req = Requirement.model_validate_json(path.read_text(encoding="utf-8"))
    content = {
        "modules": ["documents"],
        "data": ["document", "revision"],
        "interfaces": ["search", "history"],
        "states": ["saved"],
        "concurrency": "atomic revision save",
        "exceptions": ["not found"],
        "interaction": "Search documents and inspect revision history",
        "decisions": ["local only"],
        "elements": [
            {
                "id": "D-DOC",
                "sources": ["FR-001", "FR-002"],
                "description": "Document search and history",
                "acceptance_method": "search and revisions",
            }
        ],
    }
    ds = Design(
        object_id="DS-DOC",
        version=1,
        content=content,
        content_hash=fingerprint(content),
        provenance=req.provenance,
        requirement=req.ref(),
    )
    tasks = []
    for task_id, kind, goal, deps in [
        ("T03", "interface", "Document API contracts", []),
        ("T05", "implement", "Document search and revision history", ["T03"]),
        ("T06", "test", "Verify search and revision history", ["T05"]),
    ]:
        data = bundle.plan.tasks[0].semantic_content() | {
            "task_id": task_id,
            "type": kind,
            "goal": goal,
            "sources": ["FR-001", "FR-002", "D-DOC"],
            "depends_on": deps,
            "inputs": [f"artifact:{dep}" for dep in deps],
            "outputs": [f"artifact:{task_id}"],
            "write_scope": [f"documents/{task_id.lower()}"],
            "checks": [f"check:{task_id}"],
        }
        tasks.append(Task(**data, version=1, content_hash=fingerprint(data)))
    content = {
        "tasks": [{"task_id": t.task_id, "version": t.version, "content_hash": t.content_hash} for t in tasks]
    }
    plan = Plan(
        object_id=f"PLAN-{req.object_id}",
        version=1,
        content=content,
        content_hash=fingerprint(content),
        provenance=req.provenance,
        requirement=req.ref(),
        design=ds.ref(),
        tasks=tasks,
    )
    return req, ds, plan


class CapturingProvider(MockProvider):
    def invoke(self, request, step, **kwargs):
        self.request = request
        self.prompt = kwargs["prompt"]
        return super().invoke(request, step, **kwargs)


def test_full_plan_proposal_is_accepted_without_file_write_authority(bundle):
    provider = CapturingProvider(fixtures={"Daedalus:plan": {"output": bundle.plan.model_dump(mode="json")}})
    plan = DaedalusPlanner(AgentRunner(provider)).plan(bundle.requirement, bundle.design)
    assert plan.ref() == bundle.plan.ref()
    assert provider.request.permissions.allowed_write_scopes == []


@pytest.mark.parametrize("omitted", ["priority", "policy", "required", "policy.schema_version"])
def test_task_defaults_are_normalized_before_fingerprinting(omitted, bundle):
    tasks = [task.semantic_content() for task in bundle.plan.tasks]
    for task in tasks:
        if omitted == "policy.schema_version":
            task["policy"].pop("schema_version")
        else:
            task.pop(omitted)
    runner = AgentRunner(MockProvider(fixtures={"Daedalus:plan": {"output": {"tasks": tasks}}}))
    result = DaedalusPlanner(runner).plan(bundle.requirement, bundle.design)
    assert result.ref() == bundle.plan.ref()


def test_generic_semantic_plan_uses_complete_inputs_and_program_fingerprints(documents):
    req, ds, expected = documents
    draft = {"tasks": [task.semantic_content() for task in expected.tasks]}
    provider = CapturingProvider(fixtures={"Daedalus:plan": {"output": draft}})
    result = DaedalusPlanner(AgentRunner(provider)).plan(req, ds)
    assert result.ref() == expected.ref()
    serialized = json.dumps(provider.prompt, ensure_ascii=False)
    assert req.content["requirements"][0]["text"] in serialized
    assert ds.content["interaction"] in serialized
    assert validate_plan_semantics(result, req, ds).valid
    assert PlanService().prepare(req, ds, result).approved is False


@pytest.mark.parametrize("output", [{"error": "no plan"}, {"content": "plain text"}, {"tasks": []}])
def test_invalid_model_output_never_falls_back_to_campus_plan(output, documents):
    req, ds, _ = documents
    runner = AgentRunner(MockProvider(fixtures={"Daedalus:plan": {"output": output}}))
    with pytest.raises(OutputSchemaError):
        DaedalusPlanner(runner).plan(req, ds)
    assert runner.ledger.total_calls_accounted() == 1
    assert runner.records.list_by_role("Daedalus")


def test_no_provider_does_not_apply_fixture_blueprint_to_other_business(documents):
    req, ds, _ = documents
    with pytest.raises(OutputSchemaError):
        DaedalusPlanner().plan(req, ds)


def test_plan_proposal_does_not_allow_actual_artifact_write(bundle):
    payload = bundle.plan.model_dump(mode="json") | {"path": "backend/forbidden.py"}
    provider = MockProvider(fixtures={"Daedalus:plan": {"output": payload}})
    with pytest.raises(RunnerPermissionError):
        DaedalusPlanner(AgentRunner(provider)).plan(bundle.requirement, bundle.design)


def test_model_blocker_enters_common_review_and_has_complete_context(bundle):
    output = {
        "verdict": "blocked",
        "findings": [
            {
                "code": "SEMANTIC_GAP",
                "severity": "blocker",
                "task_id": "T02",
                "message": "Acceptance is not implemented",
            }
        ],
        "issues": [],
    }
    provider = CapturingProvider(fixtures={"Momus:plan_review": {"output": output}})
    reviewer = MomusPlanReviewer(runner=AgentRunner(provider))
    result = reviewer.review(bundle.plan, bundle.requirement, bundle.design)
    assert result.review.verdict == "blocked"
    assert any(item.code == "SEMANTIC_GAP" for item in result.findings)
    assert reviewer.common_reviewer.get(result.review.review_id).blocking_finding_ids
    assert not reviewer.common_reviewer.verify_for_acceptance(
        result.review.review_id, current_target=bundle.plan.ref()
    )
    serialized = json.dumps(provider.prompt, ensure_ascii=False)
    assert bundle.plan.tasks[1].goal in serialized
    assert bundle.requirement.content["requirements"][0]["acceptance"] in serialized


@pytest.mark.parametrize("output", [{"summary": "looks good"}, {"verdict": "passed", "findings": "bad"}])
def test_invalid_semantic_review_cannot_count_as_passed(output, bundle):
    reviewer = MomusPlanReviewer(
        runner=AgentRunner(MockProvider(fixtures={"Momus:plan_review": {"output": output}}))
    )
    with pytest.raises(OutputSchemaError):
        reviewer.review(bundle.plan, bundle.requirement, bundle.design)
    assert len(reviewer.common_reviewer) == 0


@pytest.mark.parametrize("dependency", ["requirement", "design", "design_requirement"])
def test_stale_dependency_blocks_validation_packaging_and_compile(dependency, bundle):
    req, ds, plan = bundle.requirement, bundle.design, bundle.plan
    old = Ref(object_id="OLD", version=1, content_hash="a" * 64)
    if dependency == "design_requirement":
        ds = ds.model_copy(update={"requirement": old})
    else:
        plan = plan.model_copy(update={dependency: old})
    report = validate_plan_semantics(plan, req, ds)
    assert not report.valid
    assert any(issue.code == "STALE_INPUT_REFERENCE" for issue in report.issues)
    with pytest.raises(PlanRejectedError):
        PlanService().prepare(req, ds, plan)
    with pytest.raises(PlanCompilationError):
        compile_plan(plan, req, ds)
    with pytest.raises(ValueError):
        build_planning_traceability(req, ds, plan)


def test_sent_cancel_without_provider_confirmation_stays_reconcilable():
    ledger = RequestLedger()
    entry = ledger.prepare(
        attempt_id="A",
        logical_task_id="L",
        run_id="R",
        task_id="T",
        role_id="Athena",
        task_kind="design",
        input_hash="a" * 64,
        idempotency_key="K",
    )
    ledger.mark_sent(entry.request_id, "provider-still-running")
    cancelled = ledger.cancel(entry.request_id)
    assert not cancelled.cancelled
    assert cancelled.status == "outcome_unknown"
    assert cancelled.needs_reconciliation
    assert ledger.query(entry.request_id).budget_held
    assert ledger.total_calls_accounted() == 1


def test_cancel_confirmation_after_unknown_does_not_refund_sent_call():
    ledger = RequestLedger()
    entry = ledger.prepare(
        attempt_id="A",
        logical_task_id="L",
        run_id="R",
        task_id="T",
        role_id="Athena",
        task_kind="design",
        input_hash="a" * 64,
        idempotency_key="K",
    )
    ledger.mark_sent(entry.request_id)
    assert ledger.cancel(entry.request_id).status == "outcome_unknown"
    confirmed = ledger.cancel(entry.request_id, provider_cancel_hook=lambda _: "cancelled")
    assert confirmed.cancelled
    assert confirmed.reason == "provider_cancelled"
    assert ledger.entries()[0].calls_consumed == 1
    assert ledger.total_calls_accounted() == 1


def test_default_mock_rejects_another_business_before_sending(documents):
    req, ds, _ = documents
    runner = AgentRunner(MockProvider())
    with pytest.raises(InputWhitelistError, match="bound"):
        DaedalusPlanner(runner).plan(req, ds)
    assert len(runner.ledger) == 0


@pytest.mark.parametrize("scope", ["../secret", "/etc/passwd", "C:\\secret"])
def test_plan_proposals_still_reject_unsafe_scopes(scope, bundle):
    draft = {"tasks": [bundle.plan.tasks[0].semantic_content() | {"write_scope": [scope]}]}
    runner = AgentRunner(MockProvider(fixtures={"Daedalus:plan": {"output": draft}}))
    with pytest.raises(RunnerPermissionError):
        DaedalusPlanner(runner).plan(bundle.requirement, bundle.design)


def test_failed_semantic_repair_stays_blocked_at_revision_limit(bundle):
    provider = MockProvider(
        fixtures={
            "Daedalus:plan": {"output": {"tasks": [task.semantic_content() for task in bundle.plan.tasks]}},
            "Momus:plan_review": {
                "output": {
                    "verdict": "blocked",
                    "findings": [
                        {
                            "code": "SEMANTIC_GAP",
                            "severity": "blocker",
                            "message": "Missing acceptance behavior",
                        }
                    ],
                }
            },
        }
    )
    runner = AgentRunner(provider)
    outcome = PlanningWorkflow(DaedalusPlanner(runner), MomusPlanReviewer(runner=runner)).run(
        bundle.requirement,
        bundle.design,
    )
    assert outcome.status == "blocked_awaiting_human"
    assert outcome.revisions_used == 1
    assert len(outcome.reviews) == 2
    assert outcome.review.verdict == "blocked"
    assert any("SEMANTIC_GAP" in finding for finding in outcome.findings)


def test_semantic_report_preserves_long_messages_and_provider_mode(bundle):
    message = "Detailed missing acceptance observation. " * 12
    provider = MockProvider(
        fixtures={
            "Momus:plan_review": {
                "output": {
                    "verdict": "blocked",
                    "findings": [{"code": "SEMANTIC_GAP", "severity": "blocker", "message": message}],
                }
            }
        }
    )
    provider.mode = "real_model"  # Transport stand-in, not an actual online model evaluation.
    reviewer = MomusPlanReviewer(runner=AgentRunner(provider))
    result = reviewer.review(bundle.plan, bundle.requirement, bundle.design)
    assert result.review.mode == "real_model"
    assert message in result.review.findings[0]
    assert message in reviewer.common_reviewer.get(result.review.review_id).submission.findings[0].message


def test_real_http_adapter_accepts_semantic_plan_without_hashes(monkeypatch, documents):
    req, ds, expected = documents
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "id": "stub-provider",
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "tasks": [task.semantic_content() for task in expected.tasks],
                                    }
                                )
                            }
                        }
                    ],
                }
            ).encode()

    def transport(request, **kwargs):
        requests.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", transport)
    runner = AgentRunner(RealProvider(api_key="test", base_url="https://test.invalid/v1", model="test"))
    plan = DaedalusPlanner(runner).plan(req, ds)
    assert plan.ref() == expected.ref()
    assert plan.provenance[0].kind == "agent_hypothesis"
    prompt = json.loads(requests[0]["messages"][1]["content"])
    assert prompt["requirement_content"] == req.content
    assert prompt["design_content"] == ds.content


@pytest.mark.scenario
def test_other_business_can_plan_review_compile_without_campus_tasks(documents):
    req, ds, expected = documents
    provider = MockProvider(
        fixtures={
            "Daedalus:plan": {"output": {"tasks": [task.semantic_content() for task in expected.tasks]}},
            "Momus:plan_review": {"output": {"verdict": "passed", "findings": [], "issues": []}},
        }
    )
    runner = AgentRunner(provider)
    out = PlanningWorkflow(DaedalusPlanner(runner), MomusPlanReviewer(runner=runner)).run(req, ds)
    assert out.status == "passed"
    assert out.plan.ref() == expected.ref()
    package = PlanService().prepare(req, ds, out.plan)
    assert package.manifest.edge_count == 2
    assert not package.approved and not package.execution_started
    assert build_planning_traceability(req, ds, out.plan).coverage_complete

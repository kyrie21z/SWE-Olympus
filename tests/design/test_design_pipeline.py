import json
from datetime import UTC, datetime

import pytest

from olympus.agents.athena import AthenaDesigner, DesignWorkflow
from olympus.agents.daedalus import DaedalusPlanner
from olympus.agents.momus.design import DesignReviewer
from olympus.agents.momus.planning import MomusPlanReviewer, PlanningWorkflow
from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError
from olympus.commands.core.bus import CommandBus
from olympus.contracts.models import Confirmation, Design, Provenance, Requirement
from olympus.orchestrator.scheduler import Scheduler
from olympus.orchestrator.state_machine import RunContext
from olympus.planning.service import PlanService
from olympus.requirements_bridge import RequirementBridge, RequirementRejected


def bridge():
    return RequirementBridge(allow_synthetic=True, clock=lambda: datetime(2030, 1, 1, tzinfo=UTC))


def workflow(bundle, *, content=None, review=None):
    provider = MockProvider(fixtures={
        "Athena:design": {"output": {"design": content or bundle.design.content}},
        "Momus:design_review": {"output": review or {"verdict": "passed", "findings": []}},
        "Daedalus:plan": {"output": {"tasks": [t.semantic_content() for t in bundle.plan.tasks]}},
        "Momus:plan_review": {"output": {"verdict": "passed", "findings": []}},
    })
    runner = AgentRunner(provider)
    admission = bridge()
    admission.admit(bundle.requirement)
    return DesignWorkflow(admission, AthenaDesigner(runner), DesignReviewer(runner, profile="campus-v1.3"))


def test_synthetic_is_explicit_and_missing_confirmation_is_rejected(bundle):
    with pytest.raises(RequirementRejected):
        RequirementBridge().admit(bundle.requirement)
    raw = bundle.requirement.model_dump(mode="json")
    raw["confirmation"] = None
    with pytest.raises(RequirementRejected):
        bridge().admit(raw)
    assert bridge().admit(bundle.requirement).synthetic


def test_future_and_stale_confirmation_are_rejected(bundle):
    with pytest.raises(RequirementRejected, match="future"):
        RequirementBridge(allow_synthetic=True,
                          clock=lambda: datetime(2020, 1, 1, tzinfo=UTC)).admit(bundle.requirement)
    data = bundle.requirement.model_dump(mode="json")
    data["version"] += 1
    with pytest.raises(ValueError, match="stale"):
        bridge().admit(data)


def test_real_confirmation_requires_trusted_server_registry(bundle):
    req = bundle.requirement.model_dump(mode="json")
    req["provenance"] = [Provenance(kind="human", source_id="upstream", locator="signed-export").model_dump()]
    confirmation = Confirmation(**(req["confirmation"] | {"origin": "human", "actor": "owner"}))
    req["confirmation"] = confirmation.model_dump(mode="json")
    with pytest.raises(RequirementRejected, match="registry"):
        RequirementBridge(authorized_actors={"owner"}, trusted_sources={"upstream"}).admit(req)
    policy = RequirementBridge(authorized_actors={"owner"}, trusted_sources={"upstream"},
                               trusted_confirmations={confirmation.confirmation_id: confirmation})
    assert not policy.admit(req).synthetic
    policy.confirmations.clear()
    with pytest.raises(RequirementRejected):
        policy.require_current(Requirement.model_validate(req))


def test_new_requirement_invalidates_previous_admission(bundle):
    policy = bridge()
    policy.admit(bundle.requirement)
    raw = bundle.requirement.model_dump(mode="json")
    raw["version"] += 1
    raw["confirmation"]["target"]["version"] += 1
    policy.admit(raw)
    with pytest.raises(RequirementRejected):
        policy.require_current(bundle.requirement)
    with pytest.raises(RequirementRejected):
        policy.admit(bundle.requirement)


def test_athena_materializes_public_design_and_preserves_full_input(bundle):
    flow = workflow(bundle)
    result = flow.generate(bundle.requirement)
    assert result.status == "awaiting_approval"
    assert result.design.ref() == bundle.design.ref()
    assert result.design.requirement == bundle.requirement.ref()
    assert result.reports[0].review.verdict == "passed"
    assert len(flow.designer.runner.ledger.entries()) == 2
    assert flow.project(bundle.requirement, result.design)["confirmation"] is None
    with pytest.raises(ValueError, match="approved"):
        flow.require_approved(bundle.requirement, result.design)


@pytest.mark.parametrize("change,code", [
    ("sms", "OUT_OF_SCOPE"), ("cancel", "CANCEL_QUEUE"),
    ("coverage", "UNCOVERED_REQUIREMENT"), ("states", "THREE_STATES"),
    ("transaction", "TRANSACTION"), ("source", "UNKNOWN_SOURCE"),
])
def test_program_design_findings_cannot_be_overridden_by_model_passed(bundle, change, code):
    data = json.loads(json.dumps(bundle.design.content))
    if change == "sms":
        data["modules"].append("短信通知")
    elif change == "cancel":
        data["elements"][1]["description"] = "CONFIRMED/WAITLISTED/CANCELLED"
    elif change == "coverage":
        data["elements"] = data["elements"][:-1]
    elif change == "states":
        data["states"].remove("CANCELLED")
    elif change == "transaction":
        data["concurrency"] = "逐条处理"
    else:
        data["elements"][0]["sources"] = ["FR-UNKNOWN"]
    flow = workflow(bundle, content=data)
    out = flow.generate(bundle.requirement, max_revisions=0)
    assert out.status == "blocked_awaiting_human"
    assert code in {f.category for f in out.reports[0].submission.findings}


def test_model_design_blocker_and_single_revision_limit(bundle):
    flow = workflow(bundle, review={"verdict": "blocked", "findings": [
        {"code": "SEMANTIC_GAP", "severity": "blocker", "message": "business meaning is missing"}]})
    out = flow.generate(bundle.requirement)
    assert out.status == "blocked_awaiting_human"
    assert out.revisions_used == 1 and out.design.version == 2
    assert len(out.reports) == 2
    assert not flow.reviewer.common.verify_for_acceptance(out.reports[0].review.review_id,
                                                          current_target=out.design.ref())
    with pytest.raises(ValueError):
        flow.generate(bundle.requirement, max_revisions=2)


def test_design_advice_is_reviewed_without_triggering_revision(bundle):
    flow = workflow(bundle, review={"verdict": "needs_revision", "findings": [
        {"code": "STYLE", "severity": "important", "message": "Clarify the response wording."},
    ]})
    out = flow.generate(bundle.requirement)
    report, = out.reports
    assert out.status == "awaiting_approval" and out.revisions_used == 0
    assert report.review.verdict == "passed" and report.blocking_finding_ids == []
    assert report.submission.findings[0].status == "open"
    assert len(flow.designer.runner.ledger.entries()) == 2

    # Stored advice-only reports from the previous gate can resume on the same version.
    old = report.submission.model_copy(update={"verdict": "needs_revision", "passed_checks": []})
    legacy = flow.reviewer.common.validate_and_record(old, current_target=out.design.ref())
    resumed = flow.reviewer.recheck(bundle.requirement, out.design, legacy, run_id="RUN-legacy")
    assert resumed.review.verdict == "passed" and resumed.review.target == out.design.ref()
    assert resumed.submission.findings == legacy.submission.findings
    assert len(flow.designer.runner.ledger.entries()) == 2


@pytest.mark.parametrize("output", [{"design": {}}, {"summary": "done"}])
def test_invalid_athena_output_never_falls_back_to_fixture(bundle, output):
    runner = AgentRunner(MockProvider(fixtures={"Athena:design": {"output": output}}))
    with pytest.raises(OutputSchemaError):
        AthenaDesigner(runner).generate(bundle.requirement)
    assert runner.ledger.total_calls_accounted() == 1


class DirectGate:
    """Test the business integration against real CommandBus, without mocking approval rules."""
    def __init__(self, bundle):
        package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
        ctx = RunContext(run=bundle.run.model_copy(deep=True))
        self.bus = CommandBus(ctx, Scheduler(bundle.plan, package.manifest, ctx))

    def start(self, **kwargs):
        self.request = kwargs
        return {"status": "interrupted", "request": kwargs}

    def approve(self):
        from olympus.contracts.models import Command
        return self.bus.dispatch(Command(
            operation_id=f"approve-{self.request['target_id']}-{self.request['expected_version']}",
            run_id=self.bus.ctx.run.run_id, actor="YOU", action="approve_design",
            target_id=self.request["target_id"], expected_version=self.request["expected_version"],
            expected_state_revision=self.bus.ctx.run.state_revision, parameters={}))


@pytest.mark.scenario
def test_design_approval_and_existing_planning_pipeline(bundle):
    flow = workflow(bundle)
    outcome = flow.generate(bundle.requirement)
    gate = DirectGate(bundle)
    assert flow.request_approval(gate, bundle.requirement, outcome.design)["status"] == "interrupted"
    assert gate.approve().status == "accepted"
    confirmation = flow.require_approved(bundle.requirement, outcome.design)
    assert confirmation.origin == "synthetic_fixture"
    runner = flow.designer.runner
    planned = flow.plan(bundle.requirement, outcome.design,
                        PlanningWorkflow(DaedalusPlanner(runner), MomusPlanReviewer(runner=runner)))
    assert planned.status == "passed"
    assert PlanService().prepare(bundle.requirement, outcome.design, planned.plan).manifest.edge_count == 12


def test_ds_version_is_independent_of_plan_and_old_approval_is_invalid(bundle):
    flow = workflow(bundle)
    first = flow.generate(bundle.requirement)
    gate = DirectGate(bundle)
    flow.request_approval(gate, bundle.requirement, first.design)
    assert gate.approve().status == "accepted"
    second = flow.revise(bundle.requirement, findings=["explicit design amendment"])
    assert second.design.version == 2 and gate.bus.scheduler.plan.version == 1
    with pytest.raises(ValueError, match="stale"):
        flow.require_approved(bundle.requirement, first.design)
    flow.request_approval(gate, bundle.requirement, second.design)
    assert gate.approve().status == "accepted"
    assert flow.require_approved(bundle.requirement, second.design).target == second.design.ref()


def test_pending_design_approval_rechecks_current_report(bundle):
    flow = workflow(bundle)
    out = flow.generate(bundle.requirement)
    gate = DirectGate(bundle)
    flow.request_approval(gate, bundle.requirement, out.design)
    # A new design invalidates the old pending request, even if the plan remains v1.
    flow.revise(bundle.requirement, findings=["explicit design amendment"])
    assert gate.approve().status == "rejected"


def test_design_dependency_mismatch_is_blocked(bundle):
    flow = workflow(bundle)
    ds = Design(**(bundle.design.model_dump(mode="json") | {
        "requirement": {**bundle.requirement.ref().model_dump(), "version": 100}}))
    assert "STALE_REQUIREMENT" in {f.code for f in flow.reviewer.program_findings(bundle.requirement, ds)}


def test_real_langgraph_design_gate_when_dependency_is_available(bundle):
    pytest.importorskip("langgraph")
    from olympus.human.store import HumanRequestStore
    from olympus.workflow.gates import Gate
    direct = DirectGate(bundle)
    gate = Gate(direct.bus, HumanRequestStore(), thread_id="p4-design")
    flow = workflow(bundle)
    result = flow.generate(bundle.requirement)
    assert flow.request_approval(gate, bundle.requirement, result.design)["status"] == "interrupted"
    assert gate.resume(answer={"decision": "approve"})["receipt_status"] == "accepted"
    flow.require_approved(bundle.requirement, result.design)


def test_duplicate_generate_does_not_spend_again_or_reset_revision_limit(bundle):
    flow = workflow(bundle)
    first = flow.generate(bundle.requirement)
    calls = len(flow.designer.runner.ledger)
    assert flow.generate(bundle.requirement).design.ref() == first.design.ref()
    assert len(flow.designer.runner.ledger) == calls
    flow.revise(bundle.requirement, findings=["authorized amendment"])
    calls = len(flow.designer.runner.ledger)
    with pytest.raises(ValueError, match="limit"):
        flow.revise(bundle.requirement, findings=["another amendment"])
    assert len(flow.designer.runner.ledger) == calls


def test_changed_pending_approval_package_gets_new_request(bundle):
    from olympus.human.store import HumanRequestStore
    store = HumanRequestStore()
    kwargs = dict(run_id="R", kind="design_approval", target_id="D", expected_version=1,
                  expected_state_revision=0, required_reports=["report-v1"], available_reports=["report-v1"])
    old = store.prepare(**kwargs)
    assert store.prepare(**kwargs).request_id == old.request_id
    new = store.prepare(**(kwargs | {"expected_version": 2, "required_reports": ["report-v2"],
                                     "available_reports": ["report-v2"]}))
    assert old.request_id != new.request_id
    assert store.load(old.request_id).status == "stale"


def test_nested_design_mutation_cannot_reuse_current_report(bundle):
    flow = workflow(bundle)
    result = flow.generate(bundle.requirement)
    result.design.content["interaction"] = "changed after review"
    with pytest.raises(ValueError, match="hash"):
        flow.request_approval(DirectGate(bundle), bundle.requirement, result.design)


def test_old_design_view_never_attaches_new_report_or_confirmation(bundle):
    flow = workflow(bundle)
    old = flow.generate(bundle.requirement)
    new = flow.revise(bundle.requirement, findings=["authorized amendment"])
    gate = DirectGate(bundle)
    flow.request_approval(gate, bundle.requirement, new.design)
    assert gate.approve().status == "accepted"
    old_view = flow.project(bundle.requirement, old.design)
    assert not old_view["is_current"] and old_view["confirmation"] is None
    assert old_view["review_id"] == old.reports[-1].review.review_id
    assert old_view["review_id"] != new.reports[-1].review.review_id
    assert flow.project(bundle.requirement, new.design)["confirmation"] is not None


def test_returned_design_cannot_mutate_the_authoritative_cached_result(bundle):
    flow = workflow(bundle)
    first = flow.generate(bundle.requirement)
    first.design.content["interaction"] = "caller changed this"
    cached = flow.generate(bundle.requirement)
    assert cached.design.content["interaction"] == bundle.design.content["interaction"]
    assert len(flow.designer.runner.ledger.entries()) == 2


def test_concurrent_generation_only_sends_one_athena_and_one_momus(bundle):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    flow = workflow(bundle)
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    invoke = flow.designer.runner.provider.invoke
    calls = []
    def blocking(request, step, **kwargs):
        calls.append(request.role_id)
        if request.role_id == "Athena":
            entered.set()
            assert release.wait(10)
        return invoke(request, step, **kwargs)
    flow.designer.runner.provider.invoke = blocking
    def second():
        second_started.set()
        return flow.generate(bundle.requirement)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(flow.generate, bundle.requirement)
        assert entered.wait(10)
        second_future = pool.submit(second)
        assert second_started.wait(10)
        release.set()
        first, duplicate = first_future.result(timeout=10), second_future.result(timeout=10)
    assert first.design.ref() == duplicate.design.ref()
    assert calls.count("Athena") == calls.count("Momus") == 1

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from olympus.agents.daedalus import DaedalusPlanner
from olympus.agents.momus.planning import (
    MomusPlanReviewer,
    PlanningWorkflow,
)
from olympus.agents.runtime import AgentRunner, ExecutionRecordService, MockProvider
from olympus.contracts.models import Design, Plan, Requirement, Task, fingerprint
from olympus.planning.validator import validate_plan_semantics
from olympus.requests import RequestLedger
from olympus.reviews.common import CommonReviewer
from olympus.traceability.planning import (
    PlanningTraceabilityIndex,
    build_planning_traceability,
)

ROOT = Path(__file__).resolve().parents[2]
PLANS = ROOT / "fixtures/plans"
sys.path.insert(0, str(ROOT / "scripts"))
from export_plans import mutate  # noqa: E402

COUNTEREXAMPLES = json.loads((PLANS / "counterexamples.json").read_text(encoding="utf-8"))


def counterexample(name):
    return Plan.model_validate(COUNTEREXAMPLES[name]["plan"])


def rebind(plan: Plan, req=None, ds=None) -> Plan:
    return Plan(
        object_id=plan.object_id,
        version=plan.version,
        content=plan.content,
        content_hash=plan.content_hash,
        provenance=plan.provenance,
        requirement=req.ref() if req is not None else plan.requirement,
        design=ds.ref() if ds is not None else plan.design,
        tasks=plan.tasks,
    )


def build_task(task_id, kind, goal, sources, deps, executor):
    data = {
        "task_id": task_id,
        "type": kind,
        "goal": goal,
        "sources": sources,
        "inputs": [f"artifact:{dep}" for dep in deps],
        "outputs": [f"artifact:{task_id}"],
        "depends_on": list(deps),
        "executor": executor,
        "reviewer": "Argus",
        "write_scope": [f"packages/{task_id.lower()}"],
        "checks": [f"check:{task_id}"],
        "resources": [],
        "priority": 0,
        "policy": {"schema_version": "1.0", "max_auto_retries": 2, "timeout_seconds": 120},
        "required": True,
    }
    return Task(**data, version=1, content_hash=fingerprint(data))


def monolithic_plan(bundle) -> Plan:
    """One implementation task lumps four requirements together; program checks pass."""
    req_ids = [item["id"] for item in bundle.requirement.content["requirements"]]
    ds_id = bundle.design.content["elements"][1]["id"]
    tasks = [
        build_task("T01", "interface", "接口类型与测试夹具", req_ids + [ds_id], [], "Hephaestus"),
        build_task("T02", "implement", "报名与取消一体化实现", req_ids[:4] + [ds_id], ["T01"], "Hephaestus"),
        build_task("T06", "test", "边界与并发测试", req_ids + [ds_id], ["T01", "T02"], "Momus"),
        build_task("T07", "integrate", "联调与恢复走查", req_ids + [ds_id], ["T02", "T06"], "Hermes"),
        build_task("T08", "doc", "验收汇总与交付清单", req_ids + [ds_id], ["T06", "T07"], "Hermes"),
    ]
    content = {
        "tasks": [
            {"task_id": task.task_id, "version": task.version, "content_hash": task.content_hash}
            for task in tasks
        ]
    }
    return Plan(
        object_id="PLAN-MONOLITHIC",
        version=1,
        content=content,
        content_hash=fingerprint(content),
        provenance=bundle.plan.provenance,
        requirement=bundle.requirement.ref(),
        design=bundle.design.ref(),
        tasks=tasks,
    )


class BrokenPlanner(DaedalusPlanner):
    """First plan drops FR-003 test coverage; the inherited revision repairs it."""

    def plan(self, req, ds):
        self.initial = mutate(
            super().plan(req, ds),
            "broken",
            {"T06": {"sources": ["FR-001", "FR-002", "FR-004", "FR-005", "D-SM-01"]}},
            version=1,
        )
        return self.initial


class OutOfScopePlanner(DaedalusPlanner):
    """Revision cannot repair a forbidden feature request, so the workflow halts."""

    def plan(self, req, ds):
        return mutate(super().plan(req, ds), "scope", {"T05": {"goal": "短信通知候补结果"}}, version=1)


def test_baseline_traceability_matrix(bundle):
    index = build_planning_traceability(bundle.requirement, bundle.design, bundle.plan)
    fixture = json.loads((PLANS / "traceability_matrix.json").read_text(encoding="utf-8"))
    assert index.model_dump(mode="json") == fixture
    assert [row.req_id for row in index.rows] == ["FR-001", "FR-002", "FR-003", "FR-004", "FR-005"]
    assert index.coverage_complete is True
    assert index.uncovered_requirements == []
    assert index.uncovered_design_elements == []
    assert index.missing_test_requirements == []
    assert index.out_of_scope_hits == []
    assert index.dangling_sources == []
    for row in index.rows:
        assert row.stage_status == "planned"
        assert row.verification_status == "unverified"
        assert row.accepted is False
        assert row.test_tasks == ["T06"]
        assert row.interface_tasks == ["T01"]


def test_row_level_requirement_design_task_links(bundle):
    index = build_planning_traceability(bundle.requirement, bundle.design, bundle.plan)
    rows = {row.req_id: row for row in index.rows}
    assert rows["FR-005"].req_text == "主动查询状态"
    assert rows["FR-005"].ds_ids == ["D-UI-01"]
    assert rows["FR-005"].implement_tasks == ["T05"]
    assert rows["FR-005"].all_tasks == ["T01", "T05", "T06", "T07", "T08"]
    assert rows["FR-003"].ds_ids == ["D-SM-01", "D-CON-01"]
    assert index.design_to_tasks["D-UI-01"] == ["T04", "T05"]
    assert index.design_to_tasks["D-DATA-01"] == []
    assert index.task_to_sources["T05"] == ["FR-005", "D-UI-01"]
    serialized = json.dumps(index.model_dump(mode="json"), ensure_ascii=False)
    assert '"passed"' not in serialized


def test_traceability_reports_missing_test_coverage(bundle):
    index = build_planning_traceability(
        bundle.requirement, bundle.design, counterexample("missing_test")
    )
    assert index.missing_test_requirements == ["FR-003"]
    assert index.coverage_complete is False
    row = next(row for row in index.rows if row.req_id == "FR-003")
    assert row.test_tasks == []
    assert row.stage_status == "planned"


def test_traceability_reports_out_of_scope_hits(bundle):
    index = build_planning_traceability(
        bundle.requirement, bundle.design, counterexample("out_of_scope_sms")
    )
    assert index.out_of_scope_hits == [
        {"task_id": "T05", "term": "短信/推送", "field": "goal"}
    ]
    assert index.coverage_complete is False


def test_traceability_reports_dangling_sources(bundle):
    index = build_planning_traceability(
        bundle.requirement, bundle.design, counterexample("missing_source")
    )
    assert index.dangling_sources == [{"task_id": "T05", "source_id": "FR-999"}]
    assert index.coverage_complete is False


def test_traceability_index_hash_is_deterministic(bundle):
    first = build_planning_traceability(bundle.requirement, bundle.design, bundle.plan)
    second = build_planning_traceability(bundle.requirement, bundle.design, bundle.plan)
    assert first.index_hash == second.index_hash
    data = first.model_dump(mode="json")
    data["index_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="index_hash"):
        PlanningTraceabilityIndex.model_validate(data)


def test_traceability_rejects_a_plan_for_another_requirement(bundle):
    other = Requirement(
        object_id="I002",
        version=1,
        content=bundle.requirement.content,
        content_hash=bundle.requirement.content_hash,
        provenance=bundle.requirement.provenance,
    )
    other_ds = Design(
        object_id="DS-I002",
        version=1,
        content=bundle.design.content,
        content_hash=bundle.design.content_hash,
        provenance=bundle.design.provenance,
        requirement=bundle.requirement.ref(),
    )
    with pytest.raises(ValueError, match="requirement"):
        build_planning_traceability(other, bundle.design, bundle.plan)
    with pytest.raises(ValueError, match="design"):
        build_planning_traceability(bundle.requirement, other_ds, bundle.plan)


def test_daedalus_uses_the_optional_runner(bundle):
    class StubRunner:
        def __init__(self):
            self.requests = []
            self.planner = DaedalusPlanner()

        def run(self, request):
            self.requests.append(request)
            return self.planner.build_plan(bundle.requirement, bundle.design)

    stub = StubRunner()
    plan = DaedalusPlanner(runner=stub).plan(bundle.requirement, bundle.design)
    assert plan.ref() == bundle.plan.ref()
    assert stub.requests[0]["role_id"] == "Daedalus"
    assert stub.requests[0]["task_kind"] == "plan"
    assert "prompt" in stub.requests[0]
    assert stub.requests[0]["out_of_scope"] == ["真实身份", "收费", "短信/推送"]


def test_revise_plan_repairs_missing_test_coverage(bundle):
    planner = DaedalusPlanner()
    broken = mutate(
        bundle.plan,
        "broken",
        {"T06": {"sources": ["FR-001", "FR-002", "FR-004", "FR-005", "D-SM-01"]}},
        version=1,
    )
    assert validate_plan_semantics(broken, bundle.requirement, bundle.design).valid is False
    assert broken.tasks[5].version == 2
    revised = planner.revise_plan(
        broken,
        ["[blocker] MISSING_TEST_COVERAGE: 需求 FR-003 没有测试任务覆盖"],
        bundle.requirement,
        bundle.design,
    )
    assert revised.version == broken.version + 1
    assert revised.tasks[5].version == 3
    assert revised.content_hash != broken.content_hash
    assert "FR-003" in revised.tasks[5].sources
    assert validate_plan_semantics(revised, bundle.requirement, bundle.design).valid is True
    assert revised.ref() != broken.ref()


def test_momus_passes_the_standard_plan(bundle):
    result = MomusPlanReviewer().review(bundle.plan, bundle.requirement, bundle.design)
    assert result.review.verdict == "passed"
    assert result.review.author == "Daedalus"
    assert result.review.reviewer == "Momus"
    assert result.review.target == bundle.plan.ref()
    assert result.review.findings == []
    assert result.review.evidence
    assert result.findings == []
    assert result.revision_recommended is False
    assert result.validation.valid is True


def test_momus_blocks_a_cyclic_plan(bundle):
    plan = counterexample("cycle")
    result = MomusPlanReviewer().review(plan, bundle.requirement, bundle.design)
    assert result.review.verdict == "blocked"
    assert any(finding.code == "CYCLE_DETECTED" for finding in result.findings)
    assert any("CYCLE_DETECTED" in finding for finding in result.review.findings)
    assert result.revision_recommended is True


def test_momus_allows_one_implementation_task_covering_multiple_requirements(bundle):
    plan = monolithic_plan(bundle)
    assert validate_plan_semantics(plan, bundle.requirement, bundle.design).valid is True
    result = MomusPlanReviewer().review(plan, bundle.requirement, bundle.design)
    assert result.review.verdict == "passed"
    assert result.record.blocking_finding_ids == []


def test_momus_independently_checks_design_source_alignment(bundle):
    content = json.loads(json.dumps(bundle.design.content))
    content["elements"][0]["sources"] = ["FR-002", "FR-999"]
    ds = Design(
        object_id=bundle.design.object_id,
        version=bundle.design.version + 1,
        content=content,
        content_hash=fingerprint(content),
        provenance=bundle.design.provenance,
        requirement=bundle.design.requirement,
    )
    plan = rebind(bundle.plan, ds=ds)
    result = MomusPlanReviewer().review(plan, bundle.requirement, ds)
    assert result.validation.valid is True
    assert result.review.verdict == "blocked"
    assert any(finding.code == "SOURCE_ALIGNMENT" for finding in result.findings)


def test_momus_reports_an_untraced_requirement(bundle):
    content = json.loads(json.dumps(bundle.requirement.content))
    content["requirements"].append({"id": "FR-006", "text": "新增需求", "acceptance": "新增需求"})
    req = Requirement(
        object_id=bundle.requirement.object_id,
        version=bundle.requirement.version + 1,
        content=content,
        content_hash=fingerprint(content),
        provenance=bundle.requirement.provenance,
    )
    plan = rebind(bundle.plan, req=req)
    result = MomusPlanReviewer().review(plan, req, bundle.design)
    assert result.review.verdict == "blocked"
    finding = next(finding for finding in result.findings if finding.code == "DS_TRACE_GAP")
    assert finding.severity == "important"
    assert "FR-006" in finding.message
    assert any(issue.code == "UNCOVERED_REQUIREMENT" for issue in result.validation.issues)


def test_close_blockers_requires_an_authorized_non_author(bundle):
    reviewer = MomusPlanReviewer(authorized_closers=["YOU", "Daedalus"])
    plan = counterexample("cycle")
    review = reviewer.review(plan, bundle.requirement, bundle.design).review
    assert review.verdict == "blocked"
    closed = reviewer.close_blockers(review, plan, closer="Momus")
    assert closed.verdict == "passed"
    assert closed.reviewer == "Momus"
    assert closed.target == plan.ref()
    assert reviewer.close_blockers(review, plan, closer="YOU").reviewer == "YOU"
    with pytest.raises(PermissionError, match="作者"):
        reviewer.close_blockers(review, plan, closer="Daedalus")
    with pytest.raises(PermissionError, match="未被授权"):
        reviewer.close_blockers(review, plan, closer="Zeus")
    passed = MomusPlanReviewer().review(bundle.plan, bundle.requirement, bundle.design).review
    with pytest.raises(ValueError, match="已通过"):
        reviewer.close_blockers(passed, bundle.plan, closer="Momus")


def test_close_blockers_rejects_a_stale_review(bundle):
    reviewer = MomusPlanReviewer()
    plan = counterexample("cycle")
    review = reviewer.review(plan, bundle.requirement, bundle.design).review
    revised = mutate(plan, "v2", {"T01": {"goal": "接口类型与测试夹具（修订）"}})
    with pytest.raises(ValueError, match="stale review"):
        reviewer.close_blockers(review, revised, closer="Momus")


def test_workflow_passes_the_standard_plan_without_revision(bundle):
    planner = DaedalusPlanner()
    outcome = PlanningWorkflow(planner).run(bundle.requirement, bundle.design)
    assert outcome.status == "passed"
    assert outcome.revisions_used == 0
    assert len(outcome.reviews) == 1
    assert outcome.review.verdict == "passed"
    assert outcome.plan.ref() == bundle.plan.ref()
    assert outcome.validation.valid is True
    assert outcome.findings == []


def test_workflow_allows_exactly_one_content_revision(bundle):
    planner = BrokenPlanner()
    outcome = PlanningWorkflow(planner, max_revisions=1).run(bundle.requirement, bundle.design)
    assert outcome.status == "passed"
    assert outcome.revisions_used == 1
    assert len(outcome.reviews) == 2
    assert outcome.plan.version == 2
    assert outcome.reviews[0].target == planner.initial.ref()
    assert outcome.reviews[0].target != outcome.plan.ref()
    assert outcome.reviews[0].verdict != "passed"
    assert outcome.review.target == outcome.plan.ref()
    assert outcome.validation.valid is True


def test_workflow_halts_after_one_revision_instead_of_looping(bundle):
    outcome = PlanningWorkflow(OutOfScopePlanner(), max_revisions=1).run(
        bundle.requirement, bundle.design
    )
    assert outcome.status == "blocked_awaiting_human"
    assert outcome.halted_reason == "REVISION_LIMIT_REACHED"
    assert outcome.revisions_used == 1
    assert len(outcome.reviews) == 2
    assert outcome.plan.version == 2
    assert outcome.review.verdict == "blocked"
    assert any("OUT_OF_SCOPE_VIOLATION" in finding for finding in outcome.findings)


def test_workflow_never_uses_a_second_revision(bundle):
    outcome = PlanningWorkflow(OutOfScopePlanner(), max_revisions=0).run(
        bundle.requirement, bundle.design
    )
    assert outcome.status == "blocked_awaiting_human"
    assert outcome.revisions_used == 0
    assert len(outcome.reviews) == 1
    assert outcome.plan.version == 1


@pytest.mark.parametrize("max_revisions", [-1, 2, 3])
def test_workflow_rejects_more_than_one_revision(max_revisions, bundle):
    with pytest.raises(ValueError, match="最多允许一轮"):
        PlanningWorkflow(DaedalusPlanner(), max_revisions=max_revisions)
    with pytest.raises(ValueError, match="最多允许一轮"):
        PlanningWorkflow(DaedalusPlanner()).run(
            bundle.requirement, bundle.design, max_revisions=max_revisions
        )


def test_workflow_requires_independent_roles(bundle):
    class SameRolePlanner(DaedalusPlanner):
        role_id = "Momus"

    with pytest.raises(ValueError, match="different roles"):
        PlanningWorkflow(SameRolePlanner())


def wired_runtime():
    ledger = RequestLedger()
    records = ExecutionRecordService()
    runner = AgentRunner(MockProvider(), ledger=ledger, records=records)
    return runner, ledger, records, CommonReviewer()


def test_daedalus_plan_records_the_call_from_a_bound_mock_proposal(bundle):
    runner, ledger, records, _ = wired_runtime()
    plan = DaedalusPlanner(runner=runner).plan(bundle.requirement, bundle.design)
    assert plan.ref() == bundle.plan.ref()
    entry = ledger.entries()[0]
    assert (entry.role_id, entry.task_kind) == ("Daedalus", "plan")
    assert (entry.logical_task_id, entry.task_id) == ("LOG-PLAN-I001", "PLAN-I001")
    assert entry.status == "succeeded"
    assert len(records.list_by_role("Daedalus")) == 1


def test_momus_review_records_the_call_and_registers_with_common_reviewer(bundle):
    runner, ledger, records, common = wired_runtime()
    result = MomusPlanReviewer(common_reviewer=common, runner=runner).review(
        bundle.plan, bundle.requirement, bundle.design
    )
    entry = ledger.entries()[0]
    assert (entry.role_id, entry.task_kind) == ("Momus", "plan_review")
    assert (entry.logical_task_id, entry.task_id) == ("LOG-REVIEW-PLAN-I001-V1", "PLAN-I001")
    assert entry.status == "succeeded"
    assert len(records.list_by_role("Momus")) == 1
    assert common.get(result.review.review_id).review.verdict == "passed"
    assert result.review.findings == []


def test_close_blockers_closes_the_recorded_common_findings(bundle):
    common = CommonReviewer()
    reviewer = MomusPlanReviewer(authorized_closers=["YOU"], common_reviewer=common)
    plan = counterexample("cycle")
    review = reviewer.review(plan, bundle.requirement, bundle.design).review
    assert common.get(review.review_id).blocking_finding_ids
    with pytest.raises(PermissionError, match="未被授权"):
        reviewer.close_blockers(review, plan, closer="Zeus")
    assert common.get(review.review_id).blocking_finding_ids
    closed = reviewer.close_blockers(review, plan, closer="YOU")
    assert closed.verdict == "passed"
    record = common.get(review.review_id)
    assert record.blocking_finding_ids == []
    assert all(finding.status == "closed" for finding in record.submission.findings)
    assert all(finding.closed_by == "YOU" for finding in record.submission.findings)
    assert all(
        finding.closure_reason == "closed via close_blockers"
        for finding in record.submission.findings
    )

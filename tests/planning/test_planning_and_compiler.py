import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from olympus.contracts.models import Plan, Task, fingerprint
from olympus.planning.compiler import (
    CompiledManifest,
    PlanCompilationError,
    StaleManifestError,
    compile_plan,
    manifest_digest,
    verify_manifest_binding,
)
from olympus.planning.models import build_task_cards
from olympus.planning.service import PlanRejectedError, PlanService
from olympus.planning.validator import validate_plan_semantics

ROOT = Path(__file__).resolve().parents[2]
PLANS = ROOT / "fixtures/plans"
sys.path.insert(0, str(ROOT / "scripts"))
from export_plans import mutate, outputs  # noqa: E402

COUNTEREXAMPLES = json.loads((PLANS / "counterexamples.json").read_text(encoding="utf-8"))
NINE_GROUPS = {
    "identity",
    "objective",
    "source_trace",
    "upstream_contract",
    "ownership",
    "output_and_scope",
    "verification_gate",
    "scheduling_hint",
    "recovery_policy",
}


def counterexample(name):
    return Plan.model_validate(COUNTEREXAMPLES[name]["plan"])


def test_standard_plan_fixture_binds_eight_tasks_and_twelve_edges(bundle):
    fixture = json.loads((PLANS / "standard_plan.json").read_text(encoding="utf-8"))
    plan = Plan.model_validate(fixture["plan"])
    assert fixture["edge_count"] == 12
    assert len(plan.tasks) == 8
    assert sum(len(task.depends_on) for task in plan.tasks) == 12
    assert len(fixture["task_cards"]) == 8
    assert plan.model_dump(mode="json") == bundle.plan.model_dump(mode="json")


def test_every_task_card_has_the_nine_groups(bundle):
    cards = build_task_cards(bundle.plan, bundle.requirement, bundle.design)
    assert [card.task_id for card in cards] == [task.task_id for task in bundle.plan.tasks]
    for card in cards:
        assert set(card.model_dump()) >= NINE_GROUPS
        assert card.ownership.executor != card.ownership.reviewer
        assert card.verification_gate.acceptance_frozen is True
        assert card.recovery_policy.on_failure == "blocked_awaiting_human"
        assert card.recovery_policy.timeout_seconds == 120
        assert card.recovery_policy.max_auto_retries == 2


def test_card_source_trace_and_test_ownership(bundle):
    cards = {card.task_id: card for card in build_task_cards(bundle.plan, bundle.requirement, bundle.design)}
    assert cards["T05"].source_trace.req_sources == ["FR-005"]
    assert cards["T05"].source_trace.ds_sources == ["D-UI-01"]
    assert cards["T05"].upstream_contract.depends_on == ["T03"]
    assert cards["T05"].upstream_contract.required_accepted_inputs == ["artifact:T03"]
    assert cards["T06"].ownership.executor == "Momus"
    assert cards["T06"].ownership.test_owner == "Momus"
    assert cards["T02"].ownership.test_owner == "Momus"
    assert cards["T01"].objective.type == "interface"
    assert cards["T08"].objective.type == "doc"


def test_baseline_plan_is_valid_and_never_accepted(bundle):
    report = validate_plan_semantics(bundle.plan, bundle.requirement, bundle.design)
    assert report.valid is True
    assert report.issues == []
    assert report.edge_count == 12
    assert report.plan_ref == bundle.plan.ref()
    serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    assert '"accepted"' not in serialized


@pytest.mark.parametrize("name", sorted(COUNTEREXAMPLES))
def test_counterexample_fixtures_report_their_expected_codes(name, bundle):
    plan = counterexample(name)
    report = validate_plan_semantics(plan, bundle.requirement, bundle.design)
    assert report.valid is False
    assert sorted({issue.code for issue in report.issues}) == sorted(
        COUNTEREXAMPLES[name]["expected_codes"]
    )
    assert all(issue.severity == "blocker" for issue in report.issues)


def test_cycle_reports_the_exact_path(bundle):
    report = validate_plan_semantics(counterexample("cycle"), bundle.requirement, bundle.design)
    issue = next(issue for issue in report.issues if issue.code == "CYCLE_DETECTED")
    assert issue.task_id == "T02"
    assert "T02 -> T05 -> T02" in issue.message


def test_missing_source_names_the_dangling_id(bundle):
    report = validate_plan_semantics(
        counterexample("missing_source"), bundle.requirement, bundle.design
    )
    issue = next(issue for issue in report.issues if issue.code == "MISSING_SOURCE")
    assert issue.task_id == "T05"
    assert issue.source_id == "FR-999"


def test_missing_test_coverage_names_the_requirement(bundle):
    report = validate_plan_semantics(
        counterexample("missing_test"), bundle.requirement, bundle.design
    )
    issue = next(issue for issue in report.issues if issue.code == "MISSING_TEST_COVERAGE")
    assert issue.source_id == "FR-003"
    row = {issue.source_id for issue in report.issues if issue.code == "MISSING_TEST_COVERAGE"}
    assert row == {"FR-003"}


def test_out_of_scope_reports_the_forbidden_term(bundle):
    report = validate_plan_semantics(
        counterexample("out_of_scope_sms"), bundle.requirement, bundle.design
    )
    issue = next(issue for issue in report.issues if issue.code == "OUT_OF_SCOPE_VIOLATION")
    assert issue.task_id == "T05"
    assert issue.source_id == "短信/推送"


def test_acyclic_plan_still_requires_real_input_supply(bundle):
    plan = counterexample("acyclic_missing_input")
    assert plan.tasks[4].depends_on == ["T01"]
    assert "artifact:T03" in plan.tasks[4].inputs
    compiled = compile_plan(plan)
    assert compiled.edge_count == 12
    report = validate_plan_semantics(plan, bundle.requirement, bundle.design)
    issue = next(issue for issue in report.issues if issue.code == "MISSING_INPUT_SUPPLY")
    assert issue.task_id == "T05"
    assert issue.source_id == "T03"
    assert sum(issue.code == "MISSING_INPUT_SUPPLY" for issue in report.issues) == 1


def test_unknown_dependency_is_reported_and_blocks_compilation(bundle):
    plan = mutate(bundle.plan, "unknown", {"T08": {"depends_on": ["T06", "T99"]}})
    report = validate_plan_semantics(plan, bundle.requirement, bundle.design)
    issue = next(issue for issue in report.issues if issue.code == "UNKNOWN_DEPENDENCY")
    assert (issue.task_id, issue.source_id) == ("T08", "T99")
    with pytest.raises(PlanCompilationError, match="unknown task T99"):
        compile_plan(plan)


def test_compiler_rejects_cycles(bundle):
    with pytest.raises(PlanCompilationError, match="dependency cycle"):
        compile_plan(counterexample("cycle"))


@pytest.mark.parametrize("scope", ["/etc/passwd", "../secret", "packages/../../etc", "C:\\temp\\x"])
def test_write_scope_rejects_absolute_and_traversal(scope, bundle):
    plan = mutate(bundle.plan, "scope", {"T05": {"write_scope": [scope]}})
    report = validate_plan_semantics(plan, bundle.requirement, bundle.design)
    issue = next(issue for issue in report.issues if issue.code == "INVALID_WRITE_SCOPE")
    assert issue.task_id == "T05"
    assert issue.source_id == scope


def test_self_review_is_rejected_by_the_task_contract(bundle):
    data = bundle.plan.tasks[0].semantic_content()
    data["reviewer"] = data["executor"]
    with pytest.raises(ValidationError, match="executor cannot review"):
        Task(**data, version=1, content_hash=fingerprint(data))


def test_manifest_matches_the_fixture_and_expected_shape(bundle):
    manifest = compile_plan(bundle.plan, bundle.requirement, bundle.design)
    fixture = json.loads((PLANS / "compiled_manifest.json").read_text(encoding="utf-8"))
    assert manifest.model_dump(mode="json") == fixture
    assert manifest.plan_ref == bundle.plan.ref()
    assert manifest.plan_hash == bundle.plan.content_hash
    assert manifest.edge_count == 12
    assert manifest.topological_order == ["T01", "T02", "T03", "T04", "T05", "T06", "T07", "T08"]
    assert manifest.layers == [["T01"], ["T02", "T03", "T04"], ["T05", "T06"], ["T07"], ["T08"]]
    assert manifest.critical_path.remaining_depth == {
        "T01": 5,
        "T02": 3,
        "T03": 4,
        "T04": 3,
        "T05": 3,
        "T06": 2,
        "T07": 2,
        "T08": 1,
    }
    assert manifest.critical_path.critical_chain == ["T01", "T03", "T05", "T07", "T08"]
    assert manifest.critical_path.priority_rank["T01"] == 1
    assert sorted(manifest.critical_path.priority_rank.values()) == list(range(1, 9))
    assert manifest.predecessors["T07"] == ["T02", "T03", "T04", "T05"]
    assert manifest.ancestors["T08"] == ["T01", "T02", "T03", "T04", "T05", "T06", "T07"]
    assert manifest.descendants["T01"] == ["T02", "T03", "T04", "T05", "T06", "T07", "T08"]
    assert manifest.task_hashes == {task.task_id: task.content_hash for task in bundle.plan.tasks}


def test_manifest_is_deterministic_and_self_consistent(bundle):
    first = compile_plan(bundle.plan, bundle.requirement, bundle.design)
    second = compile_plan(bundle.plan, bundle.requirement, bundle.design)
    assert first.manifest_hash == second.manifest_hash
    assert manifest_digest(first) == first.manifest_hash
    assert verify_manifest_binding(first, bundle.plan) is True


def test_manifest_binding_detects_a_revised_plan(bundle):
    manifest = compile_plan(bundle.plan, bundle.requirement, bundle.design)
    revised = mutate(bundle.plan, "revision", {"T02": {"goal": "报名与取消服务（修订）"}})
    with pytest.raises(StaleManifestError, match="plan_ref"):
        verify_manifest_binding(manifest, revised)
    forged = manifest.model_copy(update={"manifest_hash": "0" * 64})
    with pytest.raises(StaleManifestError, match="not self-consistent"):
        verify_manifest_binding(forged, bundle.plan)


def test_manifest_carries_no_acceptance_or_execution_state(bundle):
    manifest = compile_plan(bundle.plan, bundle.requirement, bundle.design)
    payload = json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False)
    for forbidden in ('"accepted"', '"state"', '"verification_status"', '"execution_started"'):
        assert forbidden not in payload


def test_static_conflicts_flag_nested_scopes_and_shared_resources(bundle):
    plan = mutate(
        bundle.plan,
        "conflict",
        {
            "T02": {"resources": ["db"]},
            "T04": {"write_scope": ["packages/t02/sub"], "resources": ["db"]},
        },
    )
    manifest = compile_plan(plan, bundle.requirement, bundle.design)
    pair = next(
        conflict
        for conflict in manifest.resource_conflicts.concurrent_conflicts
        if {conflict.task_a, conflict.task_b} == {"T02", "T04"}
    )
    assert pair.conflict_kinds == ["write_scope", "resource", "executor"]
    assert "packages/t02" in pair.details and "packages/t02/sub" in pair.details
    assert "db" in pair.details
    pairs = {
        frozenset((conflict.task_a, conflict.task_b))
        for conflict in manifest.resource_conflicts.concurrent_conflicts
    }
    assert frozenset(("T01", "T02")) not in pairs
    assert frozenset(("T07", "T08")) not in pairs
    assert manifest.resource_conflicts.executor_groups["Hephaestus"] == [
        "T01",
        "T02",
        "T03",
        "T04",
        "T05",
    ]


def test_plan_service_packages_valid_plans_without_approving(bundle):
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    assert package.validation.valid is True
    assert package.approved is False
    assert package.execution_started is False
    assert package.plan_ref == bundle.plan.ref()
    assert len(package.task_cards) == 8
    with pytest.raises(PlanRejectedError) as rejected:
        PlanService().prepare(
            bundle.requirement, bundle.design, counterexample("missing_test")
        )
    assert any(issue.code == "MISSING_TEST_COVERAGE" for issue in rejected.value.report.issues)


def test_fixtures_match_the_generator():
    for relative, value in outputs().items():
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        assert (ROOT / relative).read_text(encoding="utf-8") == text, relative


def test_compiled_manifest_round_trips_through_json(bundle):
    manifest = compile_plan(bundle.plan, bundle.requirement, bundle.design)
    restored = CompiledManifest.model_validate_json(manifest.model_dump_json())
    assert restored.manifest_hash == manifest.manifest_hash
    assert verify_manifest_binding(restored, bundle.plan)

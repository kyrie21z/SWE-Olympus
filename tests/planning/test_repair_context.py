"""Complete planning repairs keep review evidence and prior task contracts together."""

from copy import deepcopy

import pytest

from olympus.agents.daedalus import DaedalusPlanner
from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError
from olympus.contracts.models import Task, fingerprint
from olympus.planning.validator import validate_plan_semantics
from olympus.workflows.service import LocalPlanner, WorkflowRunner


def replace_tasks(plan, tasks):
    content = {"tasks": [{"task_id": task.task_id, "version": task.version,
                          "content_hash": task.content_hash} for task in tasks]}
    return plan.model_copy(update={"tasks": tasks, "content": content, "content_hash": fingerprint(content)})


def task_with(task, **changes):
    data = task.semantic_content() | changes
    return Task(**data, version=task.version + 1, content_hash=fingerprint(data))


class PlanningProvider(MockProvider):
    mode = "real_model"

    def __init__(self, outputs):
        super().__init__(fixtures={"Daedalus:plan": {"output": outputs[0]}})
        self.outputs, self.prompts, self.requests = outputs, [], []

    def invoke(self, request, step, **kwargs):
        self.prompts.append(kwargs["prompt"])
        self.requests.append(request)
        output = self.outputs[min(len(self.prompts) - 1, len(self.outputs) - 1)]
        return super().invoke(request, {**step, "output": output}, **kwargs)


def test_repair_context_keeps_all_active_issues_precise_gaps_and_closed_fixes(bundle):
    broken = replace_tasks(bundle.plan, [
        task_with(task, sources=[source for source in task.sources if source != "FR-003"])
        if task.type == "test" else task for task in bundle.plan.tasks
    ])
    review_context = {
        "active_findings": [
            {"finding_id": "F-block", "status": "open", "severity": "blocker", "message": "Repair state order"},
            {"finding_id": "F-advice", "status": "open", "severity": "important", "message": "Test terminal repeats"},
            {"finding_id": "F-nit", "status": "open", "severity": "nit", "message": "Style advice"},
            {"finding_id": "F-old", "status": "closed", "severity": "blocker", "message": "Old issue"},
        ],
        "resolved_findings": [
            {"finding_id": "F-fixed", "status": "closed", "severity": "blocker", "message": "Fixed range",
             "closure_reason": "T02.checks: upper bound is 10000"},
        ],
    }
    original = deepcopy(review_context)
    context = DaedalusPlanner().build_repair_context(
        broken, bundle.requirement, bundle.design, ["Repair state order"], review_context=review_context,
    )
    assert context["review_findings"] == ["Repair state order", "Test terminal repeats"]
    assert [item["finding_id"] for item in context["active_findings"]] == ["F-block", "F-advice"]
    issue, = context["program_validation"]["issues"]
    assert (issue["code"], issue["source_id"]) == ("MISSING_TEST_COVERAGE", "FR-003")
    obligation = next(item for item in context["coverage_obligations"]["requirements"]
                      if item["source_id"] == "FR-003")
    assert obligation["test_tasks"] == []
    assert obligation["covering_tasks"]
    assert context["nonregression_checklist"]["resolved_findings"][0]["closure_reason"].endswith("10000")
    context["active_findings"][0]["message"] = "locally changed"
    assert review_context == original


def test_planner_exposes_delivery_types_and_both_test_and_document_obligations(bundle):
    req_content = deepcopy(bundle.requirement.content)
    req_content["requirements"].append({"id": "NFR-DELIVERY", "text": "提供自动化测试和中文运行说明。",
                                         "acceptance": "Run the documented automated suite"})
    req = bundle.requirement.model_copy(update={"content": req_content})
    ds_content = deepcopy(bundle.design.content)
    ds_content["elements"].append({"id": "D-DELIVERY", "sources": ["NFR-DELIVERY"],
                                    "description": "提供自动化测试和中文运行说明。",
                                    "acceptance_method": "Run the documented automated suite"})
    ds = bundle.design.model_copy(update={"content": ds_content})
    manifest = LocalPlanner().build_input_manifest(req, ds)
    req_obligation = manifest["coverage_obligations"]["requirements"][-1]
    ds_obligation = manifest["coverage_obligations"]["design_elements"][-1]
    assert req_obligation["required_delivery_kinds"] == ds_obligation["required_delivery_kinds"] == ["test", "doc"]
    assert set(req_obligation["allowed_task_types"]) >= {"test", "integrate", "doc"}
    assert req_obligation["covering_tasks"] == []
    profile = manifest["execution_profile"]
    assert "read-only" in profile["checking"]
    assert "Hephaestus" in profile["deliverables"] and "documentation" in profile["deliverables"]
    assert "unrelated sources" in profile["coverage"]


def test_online_revision_receives_complete_context_and_preserves_unmodified_task_versions(bundle):
    original_tasks = [task.semantic_content() for task in bundle.plan.tasks]
    changed_tasks = deepcopy(original_tasks)
    changed_tasks[1]["checks"].extend(["Verify state ordering", "Verify terminal repeats"])
    provider = PlanningProvider([{"tasks": changed_tasks}])
    planner = LocalPlanner(AgentRunner(provider))
    revised = planner.revise_plan(
        bundle.plan, ["Verify state ordering"], bundle.requirement, bundle.design,
        repair_context={"active_findings": [
            {"finding_id": "F-advice", "status": "open", "severity": "important", "message": "Verify terminal repeats"},
        ]},
    )
    manifest = provider.requests[0].input_manifest
    assert manifest["previous_tasks"] == original_tasks
    assert manifest["review_findings"] == ["Verify state ordering", "Verify terminal repeats"]
    assert "同一轮" in manifest["prompt"]
    assert manifest["repair_context"]["program_validation"]["valid"] is True
    assert manifest["repair_context"]["nonregression_checklist"]["existing_task_ids"] == [
        task.task_id for task in bundle.plan.tasks
    ]
    assert revised.version == bundle.plan.version + 1
    assert revised.tasks[0] == bundle.plan.tasks[0]
    assert revised.tasks[1].version == bundle.plan.tasks[1].version + 1
    assert revised.tasks[1].checks[-2:] == ["Verify state ordering", "Verify terminal repeats"]
    assert set(manifest) == set(provider.requests[0].permissions.allowed_input_keys)


@pytest.mark.parametrize("reverse", [False, True])
def test_semantically_unchanged_revision_is_rejected_before_review_even_if_reordered(bundle, reverse):
    tasks = [task.semantic_content() for task in bundle.plan.tasks]
    provider = PlanningProvider([{"tasks": list(reversed(tasks)) if reverse else tasks}])
    planner = LocalPlanner(AgentRunner(provider))
    with pytest.raises(OutputSchemaError, match="no semantic change"):
        planner.revise_plan(bundle.plan, ["Fix remaining gap"], bundle.requirement, bundle.design)
    assert len(provider.prompts) == 1


def test_noop_revision_uses_one_existing_format_correction_with_actionable_hint(bundle):
    original_tasks = [task.semantic_content() for task in bundle.plan.tasks]
    fixed = deepcopy(original_tasks)
    fixed[1]["checks"].append("Verify newly required failure condition")
    provider = PlanningProvider([{"tasks": original_tasks}, {"tasks": fixed}])
    runner = WorkflowRunner(AgentRunner(provider), "repair-context-test", 10)
    revised = LocalPlanner(runner).revise_plan(
        bundle.plan, ["Verify newly required failure condition"], bundle.requirement, bundle.design,
    )
    assert len(provider.prompts) == 2
    assert "完全相同" in provider.prompts[1]["format_correction"]
    assert revised.tasks[1].checks[-1] == "Verify newly required failure condition"
    assert runner.ledger.total_calls_accounted("repair-context-test") == 2


def test_revision_program_gaps_use_one_correction_before_independent_review(bundle):
    original_tasks = [task.semantic_content() for task in bundle.plan.tasks]
    broken = deepcopy(original_tasks)
    for task in broken:
        if task["type"] == "test":
            task["sources"] = [source for source in task["sources"] if source != "FR-003"]
    fixed = deepcopy(original_tasks)
    fixed[1]["checks"].append("Verify the requested repair without losing FR-003 coverage")
    provider = PlanningProvider([{"tasks": broken}, {"tasks": fixed}])
    runner = WorkflowRunner(AgentRunner(provider), "repair-program-gaps", 10)
    revised = LocalPlanner(runner).revise_plan(
        bundle.plan, ["Fix the requested check"], bundle.requirement, bundle.design,
    )
    assert validate_plan_semantics(revised, bundle.requirement, bundle.design).valid
    assert len(provider.prompts) == 2
    hint = provider.prompts[1]["format_correction"]
    assert "MISSING_TEST_COVERAGE" in hint and "FR-003" in hint
    assert all(request.role_id == "Daedalus" for request in provider.requests)
    assert runner.ledger.total_calls_accounted("repair-program-gaps") == 2


def test_persistently_invalid_revision_exhausts_one_correction_and_cannot_reach_review(bundle):
    broken = [task.semantic_content() for task in bundle.plan.tasks]
    for task in broken:
        if task["type"] == "test":
            task["sources"] = [source for source in task["sources"] if source != "FR-003"]
    provider = PlanningProvider([{"tasks": broken}])
    runner = WorkflowRunner(AgentRunner(provider), "repair-still-blocked", 10)
    with pytest.raises(OutputSchemaError, match="deterministic blockers"):
        LocalPlanner(runner).revise_plan(
            bundle.plan, ["Fix the requested check"], bundle.requirement, bundle.design,
        )
    assert len(provider.prompts) == 2
    assert len(provider.prompts[1]["format_correction"]) < 3000
    assert all(request.role_id == "Daedalus" for request in provider.requests)
    assert runner.ledger.total_calls_accounted("repair-still-blocked") == 2


def test_initial_draft_keeps_program_review_behavior_without_revision_only_rejection(bundle):
    broken = [task.semantic_content() for task in bundle.plan.tasks]
    for task in broken:
        if task["type"] == "test":
            task["sources"] = [source for source in task["sources"] if source != "FR-003"]
    provider = PlanningProvider([{"tasks": broken}])
    proposed = LocalPlanner(AgentRunner(provider)).plan(bundle.requirement, bundle.design)
    assert not validate_plan_semantics(proposed, bundle.requirement, bundle.design).valid
    assert len(provider.prompts) == 1

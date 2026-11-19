"""Report safe exact field constraints and correct the rejected task proposal in place."""

from copy import deepcopy

import pytest

from olympus.agents.daedalus import DaedalusPlanner
from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError, RolePermissions, RoleRunInput, RunnerPermissionError
from olympus.agents.runtime.providers import ProviderStepResult
from olympus.workflows.service import LocalPlanner, WorkflowRunner


def invalid_tasks(bundle):
    rows = [task.semantic_content() for task in bundle.plan.tasks]
    rows[3]["checks"] = [f"valid-check-{index}" for index in range(5)] + ["hidden_input_value_" * 12]
    rows[2]["inputs"] = None
    rows[4]["policy"] = {"max_auto_retries": 9, "timeout_seconds": 0}
    rows[5]["hidden_secret_field_name"] = "hidden_credential_value"
    return rows


def test_plan_schema_hint_includes_safe_exact_paths_for_every_invalid_task_without_echoing_inputs(bundle):
    with pytest.raises(OutputSchemaError) as failure:
        DaedalusPlanner._materialize({"tasks": invalid_tasks(bundle)}, bundle.requirement, bundle.design, "real_model")
    hint = failure.value.hint
    assert "tasks[3].checks[5]" in hint and "≤ 160" in hint
    assert "tasks[2].inputs" in hint and "数组(list)" in hint
    assert "tasks[4].policy.max_auto_retries" in hint and "≤ 2" in hint
    assert "tasks[4].policy.timeout_seconds" in hint and "≥ 1" in hint
    assert "tasks[5].<extra_field>" in hint
    assert all(marker not in hint for marker in ("hidden_input_value", "hidden_secret_field_name", "hidden_credential_value"))
    assert "input_value" not in str(failure.value)


class ProposalCorrectionProvider(MockProvider):
    mode = "real_model"

    def __init__(self, first, corrected):
        super().__init__(fixtures={})
        self.first, self.corrected, self.prompts = first, corrected, []

    def steps(self, request):
        return [{}]

    def invoke(self, request, step, *, step_index, prompt=None):
        self.prompts.append(deepcopy(prompt))
        return ProviderStepResult(kind="model", output=self.first if len(self.prompts) == 1 else self.corrected)


def request_for(bundle, role="Daedalus", kind="plan"):
    return RoleRunInput(
        role_id=role, task_kind=kind, task_id="PLAN-SCHEMA", logical_task_id="LOG-SCHEMA",
        input_manifest={"prompt": "Return exact task proposal"}, input_refs=[bundle.requirement.ref(), bundle.design.ref()],
        output_schema=DaedalusPlanner.draft_schema(),
        permissions=RolePermissions(role_id=role, allowed_task_kinds=[kind], allowed_input_keys=["prompt"],
                                    can_propose_task_scopes=role == "Daedalus" and kind == "plan"),
    )


def test_daedalus_receives_audited_raw_proposal_and_fixes_all_fields_in_one_correction(bundle):
    bad = {"tasks": invalid_tasks(bundle), "thinking": "private-reasoning"}
    valid = {"tasks": [task.semantic_content() for task in bundle.plan.tasks]}
    provider = ProposalCorrectionProvider(bad, valid)
    runner = WorkflowRunner(AgentRunner(provider), "RUN-PLAN-SCHEMA", 5)
    runner.run(request_for(bundle), output_validator=lambda output: LocalPlanner._materialize(
        output, bundle.requirement, bundle.design, "real_model"))
    assert len(provider.prompts) == 2
    correction = provider.prompts[1]
    assert correction["previous_plan_output"] == {"tasks": bad["tasks"]}
    assert "private-reasoning" not in str(correction)
    assert "candidate_tasks" not in correction
    assert "preserve valid task IDs" in correction["plan_schema_correction"]
    assert "tasks[3].checks[5]" in correction["format_correction"]


def test_unaudited_privileged_proposal_never_enters_a_correction_prompt(bundle):
    provider = ProposalCorrectionProvider({"tasks": invalid_tasks(bundle), "call_budget": 999}, {"tasks": []})
    runner = WorkflowRunner(AgentRunner(provider), "RUN-UNAUDITED-PLAN", 5)
    with pytest.raises(RunnerPermissionError):
        runner.run(request_for(bundle), output_validator=lambda output: LocalPlanner._materialize(
            output, bundle.requirement, bundle.design, "real_model"))
    assert len(provider.prompts) == 1 and "previous_plan_output" not in provider.prompts[0]


def test_plan_field_hints_are_capped_and_required_fields_are_explicit(bundle):
    rows = [task.semantic_content() for task in bundle.plan.tasks]
    for row in rows:
        row["checks"] = ["x" * 161 for _ in range(3)]
        del row["inputs"]
    with pytest.raises(OutputSchemaError) as failure:
        DaedalusPlanner._materialize({"tasks": rows}, bundle.requirement, bundle.design, "real_model")
    assert "tasks[0].inputs: 必填字段(required)" in failure.value.hint
    assert failure.value.hint.count("tasks[") == 8
    assert "另有" in failure.value.hint

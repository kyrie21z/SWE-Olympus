"""Keep valid revision progress between bounded Daedalus capability corrections."""

from copy import deepcopy

import pytest

from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError, RolePermissions, RoleRunInput
from olympus.agents.runtime.models import UnknownOutcomeError
from olympus.agents.runtime.providers import ProviderStepResult
from olympus.contracts.models import Plan, Task, fingerprint
from olympus.workflows.service import WorkflowRunner


def candidate(plan, **goals):
    tasks = []
    for task in plan.tasks:
        if task.task_id in goals:
            content = {**task.semantic_content(), "goal": goals[task.task_id]}
            task = Task(**content, version=task.version + 1, content_hash=fingerprint(content))
        tasks.append(task)
    content = {"tasks": [{"task_id": task.task_id, "version": task.version, "content_hash": task.content_hash}
                         for task in tasks]}
    return Plan.model_validate({**plan.model_dump(mode="json"), "tasks": tasks,
                                "content": content, "content_hash": fingerprint(content)})


class CandidateProvider(MockProvider):
    mode = "real_model"

    def __init__(self, outcomes):
        super().__init__(fixtures={})
        self.outcomes = outcomes
        self.requests, self.prompts = [], []

    def steps(self, request):
        return [{}]

    def invoke(self, request, step, *, step_index, prompt=None):
        outcome = self.outcomes[len(self.requests)]
        self.requests.append(request)
        self.prompts.append(deepcopy(prompt))
        if outcome == "unknown":
            return ProviderStepResult(kind="model", status="outcome_unknown", error_code="provider_timeout")
        return ProviderStepResult(kind="model", output={"revision": outcome})


def request_for(bundle, *, role="Daedalus", kind="plan"):
    return RoleRunInput(
        role_id=role, task_kind=kind, task_id="PLAN-CANDIDATE", logical_task_id="LOG-CANDIDATE",
        input_refs=[bundle.requirement.ref(), bundle.design.ref(), bundle.plan.ref()],
        input_manifest={"previous_tasks": [task.semantic_content() for task in bundle.plan.tasks]},
        permissions=RolePermissions(role_id=role, allowed_task_kinds=[kind], allowed_input_keys=["previous_tasks"]),
    )


def rejecting(candidate_plan, hint):
    error = OutputSchemaError("candidate still has a capability conflict", hint=hint)
    error.plan_candidate = candidate_plan
    return error


def test_plan_corrections_use_latest_candidate_and_fresh_budget_without_losing_fixed_parts(bundle):
    first = candidate(bundle.plan, T01="Already repaired A")
    second = candidate(first, T02="Already repaired B")
    provider = CandidateProvider([1, 2, 3])
    runner = WorkflowRunner(AgentRunner(provider), "RUN-CANDIDATE", 3)
    saved = []
    runner.on_plan_candidate = saved.append
    request = request_for(bundle)

    def validate(output):
        if output["revision"] == 1:
            raise rejecting(first, "Remaining issue B")
        if output["revision"] == 2:
            raise rejecting(second, "Remaining issue C")

    result = runner.run(request, output_validator=validate)
    assert result.structured_output == {"revision": 3}
    assert saved == [first, second]
    assert provider.prompts[1]["candidate_tasks"] == [task.semantic_content() for task in first.tasks]
    assert provider.prompts[2]["candidate_tasks"] == [task.semantic_content() for task in second.tasks]
    assert provider.prompts[2]["previous_tasks"] == provider.prompts[2]["candidate_tasks"]
    assert provider.prompts[2]["format_correction"] == "Remaining issue C"
    assert "preserve" in provider.prompts[2]["candidate_correction"]
    assert request.input_manifest["previous_tasks"] == [task.semantic_content() for task in bundle.plan.tasks]
    assert [request.budget.calls_used for request in provider.requests] == [0, 1, 2]
    keys = [request.idempotency_key for request in provider.requests]
    assert keys[1] == keys[0] + "-format" and keys[2] == keys[0] + "-format2"
    assert runner.ledger.total_calls_accounted("RUN-CANDIDATE") == 3


@pytest.mark.parametrize("budget", [1, 2, 3])
def test_plan_correction_limit_and_budget_hold_even_when_candidates_keep_failing(bundle, budget):
    proposal = candidate(bundle.plan, T01="Preserve this repair")
    provider = CandidateProvider([1, 2, 3])
    runner = WorkflowRunner(AgentRunner(provider), "RUN-BOUNDED-CANDIDATE", budget)
    saved = []
    runner.on_plan_candidate = saved.append

    def validate(output):
        raise rejecting(proposal, "Remaining issue")

    with pytest.raises(OutputSchemaError) as failure:
        runner.run(request_for(bundle), output_validator=validate)
    assert failure.value.role_id == "Daedalus"
    assert len(provider.requests) == budget
    assert len(saved) == budget  # Preserve the final proposal even when no further call is permitted.
    assert runner.ledger.total_calls_accounted("RUN-BOUNDED-CANDIDATE") == budget


@pytest.mark.parametrize(("role", "kind", "attach_candidate"), [
    ("Momus", "acceptance_definition", True), ("Daedalus", "plan", False),
])
def test_other_roles_and_non_candidate_schema_errors_still_get_only_one_correction(bundle, role, kind, attach_candidate):
    provider = CandidateProvider([1, 2, 3])
    runner = WorkflowRunner(AgentRunner(provider), "RUN-ONE-CORRECTION", 10)

    def validate(output):
        error = OutputSchemaError("invalid output", hint="Fix structure")
        if attach_candidate:
            error.plan_candidate = bundle.plan
        raise error

    with pytest.raises(OutputSchemaError):
        runner.run(request_for(bundle, role=role, kind=kind), output_validator=validate)
    assert len(provider.requests) == 2
    assert "candidate_tasks" not in provider.prompts[-1]


@pytest.mark.parametrize("after_candidate", [False, True])
def test_unknown_outcome_never_triggers_another_correction(bundle, after_candidate):
    provider = CandidateProvider([1, "unknown", 3] if after_candidate else ["unknown", 2, 3])
    runner = WorkflowRunner(AgentRunner(provider), "RUN-UNKNOWN-CANDIDATE", 10)

    def validate(output):
        raise rejecting(bundle.plan, "Remaining issue")

    with pytest.raises(UnknownOutcomeError):
        runner.run(request_for(bundle), output_validator=validate)
    assert len(provider.requests) == (2 if after_candidate else 1)
    assert runner.ledger.entries()[-1].status == "outcome_unknown"

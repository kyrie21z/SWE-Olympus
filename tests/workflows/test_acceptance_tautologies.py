"""A short-circuit success branch cannot hide a failed evidence predicate."""

import pytest

from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError
from olympus.agents.runtime.providers import ProviderStepResult
from olympus.workflows.acceptance import generate_acceptance, validate_check
from olympus.workflows.service import WorkflowRunner

from test_acceptance_corrections import plan_with, task_with


@pytest.mark.parametrize("expression", [
    "predicate or True",
    "True or predicate",
    "predicate or 1",
    "predicate or 'fallback'",
    "predicate or [False]",
    "predicate or {'verified': False}",
    "predicate or (other or True)",
    "(predicate and False) or True",
    "not (predicate and False)",
    "(predicate or True) and (other or 'fallback')",
    "any(token in text for token in command) or True",
])
def test_proven_true_short_circuit_cannot_replace_actual_evidence(expression):
    with pytest.raises(OutputSchemaError, match="provably always true") as failure:
        validate_check(f"assert {expression}\n", [])
    assert "or True" in failure.value.hint
    assert "实际" in failure.value.hint


@pytest.mark.parametrize("expression", [
    "predicate",
    "predicate or other",
    "predicate or False",
    "False or predicate",
    "predicate and True",
    "True and predicate",
    "(predicate or True) and other",
    "predicate or []",
    "predicate or {}",
    "not predicate",
    "result is not None",
    "' '.join(command) in text",
])
def test_unknown_behavior_predicates_are_not_rejected_as_tautologies(expression):
    validate_check(f"assert {expression}\n", [])


REPORT_CHECK = (
    "report = source_metadata['reports/integration/check-report.json']['data']\n"
    "command = report['checks'][0]['command']\n"
    "text = source_metadata['README.md']['text']\n"
    "assert ' '.join(command) in text\n"
)


class CommandCorrectionProvider(MockProvider):
    mode = "real_model"

    def __init__(self):
        super().__init__(fixtures={})
        self.prompts = []

    def steps(self, request):
        return [{}]

    def invoke(self, request, step, *, step_index, prompt=None):
        self.prompts.append(prompt)
        task = request.input_manifest["tasks"][0]
        source = REPORT_CHECK if len(self.prompts) > 1 else REPORT_CHECK.rstrip() + " or True\n"
        return ProviderStepResult(kind="model", output={"tasks": [{
            "task_id": task["task_id"], "checks": {task["checks"][0]: source},
        }]})


def test_report_command_tautology_gets_one_correction_to_real_evidence(bundle):
    task = task_with(bundle.plan.tasks[0], type="doc", executor="Hermes",
                     write_scope=["reports/docs"], outputs=["reports/docs/check-report.json"],
                     checks=["Verify README against the actual command evidence"])
    provider = CommandCorrectionProvider()
    runner = WorkflowRunner(AgentRunner(provider), "RUN-COMMAND-TAUTOLOGY", 5)

    result = generate_acceptance(runner, bundle.requirement, bundle.design, plan_with(bundle.plan, [task]))

    assert len(provider.prompts) == 2
    assert "or True" in provider.prompts[1]["format_correction"]
    assert "or True" in provider.prompts[1]["previous_output"]["tasks"][0]["checks"][task.checks[0]]
    assert result[task.task_id]["checks"][task.checks[0]] == REPORT_CHECK
    assert runner.ledger.total_calls_accounted("RUN-COMMAND-TAUTOLOGY") == 2

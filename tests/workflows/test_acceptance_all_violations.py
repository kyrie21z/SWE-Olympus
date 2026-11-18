"""A single correction receives every policy violation, including shared setup errors."""

from copy import deepcopy

import pytest

from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError
from olympus.agents.runtime.providers import ProviderStepResult
from olympus.workflows.acceptance import generate_acceptance, validate_check
from olympus.workflows.service import WorkflowRunner

from test_acceptance_corrections import plan_with, task_with

BAD_SETUP = (
    "import re\nimport copy\n"
    "class Factory:\n    def _make(self):\n        return {'value': 2}\n"
    "factory = Factory()\n"
)
BAD_BODY = "assert factory._make()['value'] == 2\nassert getattr(factory, 'value', 2) == 2\n"


def test_one_ast_scan_reports_imports_private_attribute_and_forbidden_builtin_together():
    with pytest.raises(OutputSchemaError) as failure:
        validate_check(BAD_SETUP + BAD_BODY, [])
    assert str(failure.value) == "acceptance uses an unregistered module"
    hint = failure.value.hint
    assert all(text in hint for text in ("导入 re", "导入 copy", "_make", "禁止使用 getattr"))
    assert len(failure.value.validation_hints) == 4


class FixAllProvider(MockProvider):
    mode = "real_model"

    def __init__(self):
        super().__init__(fixtures={})
        self.prompts = []

    def steps(self, request):
        return [{}]

    def invoke(self, request, step, *, step_index, prompt=None):
        self.prompts.append(deepcopy(prompt))
        task = request.input_manifest["tasks"][0]
        if len(self.prompts) == 1:
            output = {"tasks": [{"task_id": task["task_id"], "setup": BAD_SETUP,
                                  "checks": {check: BAD_BODY for check in task["checks"]}}]}
        else:
            hint = prompt["format_correction"]
            assert all(text in hint for text in ("导入 re", "导入 copy", "_make", "禁止使用 getattr"))
            # Common setup diagnostics are grouped across checks instead of repeated ten times.
            assert hint.count("导入 re") == hint.count("导入 copy") == 1
            assert "check:first" in hint and "check:second" in hint
            assert prompt["previous_output"]["tasks"][0]["setup"] == BAD_SETUP
            output = {"tasks": [{"task_id": task["task_id"], "setup": "def make_service():\n    return {'value': 2}\n",
                                  "checks": {check: "assert make_service()['value'] == 2\n"
                                             for check in task["checks"]}}]}
        return ProviderStepResult(kind="model", output=output)


def test_shared_re_copy_and_private_helper_errors_are_all_corrected_in_one_call(bundle):
    task = task_with(bundle.plan.tasks[0], type="implement", checks=["check:first", "check:second"])
    provider = FixAllProvider()
    runner = WorkflowRunner(AgentRunner(provider), "RUN-ALL-VIOLATIONS", 5)
    result = generate_acceptance(runner, bundle.requirement, bundle.design, plan_with(bundle.plan, [task]))
    assert len(provider.prompts) == 2
    assert set(result[task.task_id]["checks"]) == set(task.checks)
    for source in result[task.task_id]["checks"].values():
        assert "import re" not in source and "import copy" not in source and "._make" not in source
        exec(compile(source, "<corrected-complete-check>", "exec"), {})


def test_multiple_violation_hints_are_bounded_but_rejection_is_preserved():
    source = "\n".join(f"import forbidden_module_{index}" for index in range(12)) + "\nassert 1 + 1 == 2\n"
    with pytest.raises(OutputSchemaError) as failure:
        validate_check(source, [])
    assert len(failure.value.validation_hints) == 8
    assert "另有 4 项" in failure.value.hint

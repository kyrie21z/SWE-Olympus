"""Acceptance corrections retain the exact audited candidate instead of rewriting blind."""

from copy import deepcopy
import json

import pytest

from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError, RolePermissions, RoleRunInput
from olympus.agents.runtime.providers import ProviderStepResult
from olympus.requests import RequestLedger
from olympus.workflows.acceptance import _task_manifest, generate_acceptance
from olympus.workflows.service import WorkflowRunner

from test_acceptance_corrections import plan_with, task_with


class TargetedProvider(MockProvider):
    mode = "real_model"

    def __init__(self, output, *, acceptance=True):
        super().__init__(fixtures={})
        self.output, self.acceptance = output, acceptance
        self.prompts = []

    def steps(self, request):
        return [{}]

    def invoke(self, request, step, *, step_index, prompt=None):
        self.prompts.append(deepcopy(prompt))
        if len(self.prompts) == 1:
            output = self.output
        elif self.acceptance:
            # The correction makes a surgical edit to the actual rejected result.
            output = deepcopy(prompt["previous_output"])
            output["tasks"][0]["setup"] = ""
        else:
            assert "previous_output" not in prompt
            output = {"ok": True}
        return ProviderStepResult(kind="model", output=output)


def test_rejected_script_is_returned_for_targeted_correction_and_archived_without_inputs_or_reasoning(tmp_path, bundle):
    task = task_with(bundle.plan.tasks[0], checks=["check:symbols"], write_scope=["inventory_service_interface.py"])
    body = "assert 'InventoryService' in source_metadata['inventory_service_interface.py']['public_symbols']\n"
    output = {"reasoning": "private-reasoning-must-be-removed", "tasks": [{
        "task_id": task.task_id, "setup": "import source_metadata\n", "checks": {task.checks[0]: body},
        "thinking": "row-reasoning-must-be-removed",
    }]}
    provider = TargetedProvider(output)
    ledger = RequestLedger(tmp_path / "model-requests.json")
    runner = WorkflowRunner(AgentRunner(provider, ledger=ledger), "RUN-TARGETED", 10)
    frozen = generate_acceptance(runner, bundle.requirement, bundle.design, plan_with(bundle.plan, [task]))
    previous = provider.prompts[1]["previous_output"]
    assert previous == {"tasks": [{"task_id": task.task_id, "setup": "import source_metadata\n",
                                    "checks": {task.checks[0]: body}}]}
    assert "preserve valid" in provider.prompts[1]["acceptance_correction"]
    assert frozen[task.task_id]["checks"][task.checks[0]] == body
    files = list((tmp_path / "acceptance-candidates").glob("*.json"))
    assert len(files) == 1 and len(files[0].stem) == 64
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert saved == previous
    assert set(saved) == {"tasks"}
    assert "reasoning" not in files[0].read_text(encoding="utf-8")
    assert "requirement_content" not in files[0].read_text(encoding="utf-8")


@pytest.mark.parametrize(("role", "kind"), [("Athena", "design"), ("Daedalus", "plan"), ("Momus", "plan_review")])
def test_other_role_kinds_do_not_receive_or_archive_rejected_output(tmp_path, bundle, role, kind):
    provider = TargetedProvider({"valid": False, "thinking": "private"}, acceptance=False)
    runner = WorkflowRunner(AgentRunner(provider, ledger=RequestLedger(tmp_path / "model-requests.json")),
                            "RUN-OTHER-KIND", 10)
    request = RoleRunInput(
        role_id=role, task_kind=kind, task_id="TASK", logical_task_id="LOG",
        input_manifest={"input_marker": "input-not-to-save"}, input_refs=[bundle.plan.ref()],
        permissions=RolePermissions(role_id=role, allowed_task_kinds=[kind], allowed_input_keys=["input_marker"]),
    )

    def validate(output):
        if not output.get("ok"):
            raise OutputSchemaError("invalid", hint="Return ok")

    runner.run(request, output_validator=validate)
    assert len(provider.prompts) == 2 and "previous_output" not in provider.prompts[1]
    assert not (tmp_path / "acceptance-candidates").exists()


def test_interface_prompt_requires_flat_injected_metadata_checks_and_general_helpers_use_public_names(bundle):
    task = task_with(bundle.plan.tasks[0], write_scope=["inventory_service_interface.py"])
    prompt = _task_manifest(bundle.requirement, bundle.design, task, [])["prompt"]
    assert "INTERFACE STAGE" in prompt and "flat, top-level assertions" in prompt
    assert "Do not create unittest classes" in prompt
    assert "make_service" in prompt and "not private names such as _make" in prompt

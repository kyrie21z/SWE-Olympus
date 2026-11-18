"""Compact shared setup is expanded before policy validation and freezing."""

import pytest

from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError
from olympus.workflows.acceptance import _task_manifest, generate_acceptance

from test_acceptance_corrections import plan_with, task_with


def generate(bundle, task, row):
    provider = MockProvider(fixtures={f"Momus:acceptance_definition:{task.task_id}": {
        "output": {"tasks": [{"task_id": task.task_id, **row}]},
    }})
    return generate_acceptance(AgentRunner(provider), bundle.requirement, bundle.design,
                               plan_with(bundle.plan, [task]))[task.task_id]


def test_shared_setup_expands_into_each_independent_frozen_script(bundle):
    task = task_with(bundle.plan.tasks[0], checks=["check:square", "check:root"])
    setup = "import math\ndef square(value):\n    return value * value\n"
    bodies = {"check:square": "assert square(3) == 9\n", "check:root": "assert math.sqrt(square(4)) == 4\n"}
    frozen = generate(bundle, task, {"setup": setup, "checks": bodies})
    FrozenAcceptance.model_validate(frozen).verify(task)
    assert frozen["checks"] == {check: setup + "\n\n" + body for check, body in bodies.items()}
    for source in frozen["checks"].values():
        exec(compile(source, "<frozen-independent-check>", "exec"), {})
    schema = _task_manifest(bundle.requirement, bundle.design, task, [])["output_schema"]
    row_schema = schema["properties"]["tasks"]["items"]
    assert row_schema["properties"]["setup"] == {"type": "string"}
    assert "setup" not in row_schema["required"]


@pytest.mark.parametrize("include_setup", [False, True])
def test_legacy_or_empty_setup_preserves_frozen_source_exactly(bundle, include_setup):
    task = task_with(bundle.plan.tasks[0], checks=["check:legacy"])
    source = "import math\nassert math.sqrt(4) == 2\n"
    row = {"checks": {"check:legacy": source}}
    if include_setup:
        row["setup"] = ""
    assert generate(bundle, task, row)["checks"]["check:legacy"] == source


@pytest.mark.parametrize(("setup", "body", "hint"), [
    (None, "assert 1 + 1 == 2\n", "setup"),
    ("import sys\n", "assert 1 + 1 == 2\n", "sys"),
    ("value = object().__class__\n", "assert value is not None\n", "__class__"),
    ("source_metadata = {}\n", "assert 1 + 1 == 2\n", "只读证据"),
    ("#" + "x" * 40000 + "\n", "#" + "y" * 30000 + "\nassert 1 + 1 == 2\n", "65536"),
], ids=["null-setup", "unregistered-import", "private-reflection", "metadata-rebinding", "assembled-too-large"])
def test_invalid_or_privileged_setup_and_assembled_size_are_rejected_before_freezing(bundle, setup, body, hint):
    task = task_with(bundle.plan.tasks[0], checks=["check:guarded"])
    with pytest.raises(OutputSchemaError) as failure:
        generate(bundle, task, {"setup": setup, "checks": {"check:guarded": body}})
    assert hint in failure.value.hint

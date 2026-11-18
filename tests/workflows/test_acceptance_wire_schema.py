"""A single known task can use strict remote output without breaking local fixtures."""

from copy import deepcopy
from io import BytesIO
import json

import pytest

from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError, RealProvider
from olympus.agents.runtime.models import RolePermissions, RoleRunInput
from olympus.workflows.acceptance import _task_manifest, generate_acceptance

from test_acceptance_corrections import plan_with, task_with


SOURCE = "assert 'ISearchAPI.search' in source_metadata['interfaces/ISearchAPI.py']['signatures']\n"


def task_for(bundle):
    return task_with(bundle.plan.tasks[0], write_scope=["interfaces/ISearchAPI.py"],
                     outputs=["interfaces/ISearchAPI.py"], checks=["Check search signature", "Check exact public name"])


def output_for(task, *, setup=False):
    row = {"task_id": task.task_id, "checks": {check: SOURCE for check in task.checks}}
    if setup:
        row["setup"] = ""
    return {"tasks": [row]}


def test_local_fixture_without_setup_remains_valid_with_exact_task_and_checks(bundle):
    task = task_for(bundle)
    provider = MockProvider(fixtures={"Momus:acceptance_definition": {"output": output_for(task)}})
    result = generate_acceptance(AgentRunner(provider), bundle.requirement, bundle.design, plan_with(bundle.plan, [task]))
    schema = _task_manifest(bundle.requirement, bundle.design, task, [])["output_schema"]
    row = schema["properties"]["tasks"]["items"]
    assert "setup" not in row["required"]
    assert row["properties"]["task_id"]["enum"] == [task.task_id]
    assert row["properties"]["checks"]["additionalProperties"] is False
    assert row["properties"]["checks"]["required"] == task.checks
    assert set(row["properties"]["checks"]["properties"]) == set(task.checks)
    assert result[task.task_id]["checks"] == {check: SOURCE for check in task.checks}


def test_wire_copy_is_strict_and_required_setup_without_mutating_local_schema(bundle, monkeypatch):
    task = task_for(bundle)
    manifest = _task_manifest(bundle.requirement, bundle.design, task, [])
    local_before = deepcopy(manifest["output_schema"])
    request = RoleRunInput(
        role_id="Momus", task_kind="acceptance_definition", task_id=task.task_id,
        logical_task_id="acceptance-wire-test", input_manifest=manifest, input_refs=[bundle.plan.ref()],
        output_schema=manifest["output_schema"], permissions=RolePermissions(
            role_id="Momus", allowed_task_kinds=["acceptance_definition"], allowed_input_keys=list(manifest)),
    )
    calls = []

    def transport(http_request, **kwargs):
        calls.append(json.loads(http_request.data))
        body = {"choices": [{"message": {"content": json.dumps(output_for(task, setup=True))}}]}
        return BytesIO(json.dumps(body).encode())

    monkeypatch.setattr("urllib.request.urlopen", transport)
    provider = RealProvider(api_key="test", base_url="https://maas.qianwenaiapi.com/compatible-mode/v1", model="qwen4-max")
    result = provider.invoke(request, {}, step_index=1)

    assert result.status == "succeeded" and len(calls) == 1
    response_format = calls[0]["response_format"]
    assert response_format["type"] == "json_schema" and response_format["json_schema"]["strict"] is True
    tasks = response_format["json_schema"]["schema"]["properties"]["tasks"]
    assert tasks["minItems"] == tasks["maxItems"] == 1
    row = tasks["items"]
    assert set(row["required"]) == {"task_id", "checks", "setup"}
    assert row["properties"]["setup"] == {"type": "string"}
    assert row["properties"]["task_id"]["enum"] == [task.task_id]
    assert set(row["properties"]["checks"]["required"]) == set(task.checks)
    assert row["properties"]["checks"]["additionalProperties"] is False
    assert request.output_schema == manifest["output_schema"] == local_before


def test_complete_extra_task_output_is_still_rejected_locally(bundle):
    task = task_for(bundle)
    output = output_for(task)
    output["tasks"].append(deepcopy(output["tasks"][0]))
    provider = MockProvider(fixtures={"Momus:acceptance_definition": {"output": output}})
    with pytest.raises(OutputSchemaError, match="requested task exactly once"):
        generate_acceptance(AgentRunner(provider), bundle.requirement, bundle.design, plan_with(bundle.plan, [task]))

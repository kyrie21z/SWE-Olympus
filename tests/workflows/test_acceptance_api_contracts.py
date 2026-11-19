"""Freeze acceptance against declared ABI, including task and ancestor obligations."""

from copy import deepcopy

import pytest

from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError
from olympus.agents.runtime.providers import ProviderStepResult
from olympus.workflows.acceptance import (
    _available_api_contracts,
    _input_tasks,
    _task_manifest,
    generate_acceptance,
    migrate_acceptance_checkpoint,
)
from olympus.workflows.service import WorkflowRunner

from test_acceptance_corrections import plan_with, task_with

CONTRACTS = {"modules/Store.py": {"Store": ["save_document"], "MissingDocument": []}}
GOOD = "from modules.Store import Store\nstore = Store()\nassert store.save_document('x') == 'x'\n"
BAD = "from modules.Store import DocumentStore\nstore = DocumentStore()\nassert store.save('x') == 'x'\n"


def abi_plan(bundle):
    author = task_with(bundle.plan.tasks[0], type="implement", executor="Hephaestus", reviewer="Argus",
                       write_scope=["modules/Store.py"], outputs=["modules/Store.py"],
                       inputs=[], depends_on=[], checks=["Store.save_document returns saved value"],
                       api_contracts=CONTRACTS)
    checker = task_with(bundle.plan.tasks[1], type="test", executor="Momus", reviewer="Argus",
                        write_scope=["reports/store"], outputs=["artifact:store-tests"],
                        inputs=["modules/Store.py"], depends_on=[author.task_id], checks=["save roundtrip"])
    return plan_with(bundle.plan, [author, checker])


class ABIProvider(MockProvider):
    mode = "real_model"

    def __init__(self, checker_id):
        super().__init__(fixtures={})
        self.checker_id, self.prompts = checker_id, []

    def steps(self, request):
        return [{}]

    def invoke(self, request, step, *, step_index, prompt=None):
        self.prompts.append(deepcopy(prompt))
        task = prompt["tasks"][0]
        if task["task_id"] == self.checker_id:
            script = GOOD if "format_correction" in prompt else BAD
        else:
            script = "assert 'Store' in source_metadata['modules/Store.py']['public_symbols']\n"
        return ProviderStepResult(kind="model", output={"tasks": [{
            "task_id": task["task_id"], "checks": {check: script for check in task["checks"]},
        }]})


def test_current_and_available_input_manifest_include_full_checks_and_authoritative_abi(bundle):
    plan = abi_plan(bundle)
    author, checker = plan.tasks
    manifest = _task_manifest(bundle.requirement, bundle.design, checker, [author])
    assert manifest["tasks"][0]["checks"] == checker.checks
    assert manifest["available_inputs"][0]["checks"] == author.checks
    assert manifest["available_inputs"][0]["api_contracts"] == CONTRACTS
    assert manifest["api_contracts"] == CONTRACTS
    assert "authoritative exact" in manifest["prompt"] and "never invent aliases" in manifest["prompt"]


def test_one_correction_replaces_wrong_abi_aliases_before_freezing_and_preserves_other_task(bundle):
    plan = abi_plan(bundle)
    author, checker = plan.tasks
    provider = ABIProvider(checker.task_id)
    runner = WorkflowRunner(AgentRunner(provider), "RUN-API-CONTRACT-CORRECTION", 4)
    result = generate_acceptance(runner, bundle.requirement, bundle.design, plan)
    calls = [prompt for prompt in provider.prompts if prompt["tasks"][0]["task_id"] == checker.task_id]
    assert len(provider.prompts) == 3 and len(calls) == 2
    assert "DocumentStore" in calls[1]["format_correction"]
    assert "Store" in calls[1]["format_correction"]
    assert calls[1]["previous_output"]["tasks"][0]["checks"] == {checker.checks[0]: BAD}
    assert result[checker.task_id]["checks"] == {checker.checks[0]: GOOD}
    FrozenAcceptance.model_validate(result[author.task_id]).verify(author)


def test_completed_and_migrated_checkpoints_are_rechecked_against_declared_abi(bundle):
    plan = abi_plan(bundle)
    checker = plan.tasks[1]
    bad = FrozenAcceptance.freeze(checker, {checker.checks[0]: BAD}).model_dump(mode="json")
    provider = ABIProvider(checker.task_id)
    with pytest.raises(OutputSchemaError):
        generate_acceptance(AgentRunner(provider), bundle.requirement, bundle.design, plan,
                            completed={checker.task_id: bad})
    assert provider.prompts == []
    assert migrate_acceptance_checkpoint(plan, plan, {checker.task_id: bad}, plan.ref().model_dump(mode="json")) == {}


def test_latest_same_file_declaration_is_authoritative_without_union_of_removed_methods(bundle):
    plan = abi_plan(bundle)
    author, checker = plan.tasks
    next_contract = {"modules/Store.py": {"Store": ["store_document"]}}
    second = task_with(author, task_id="STORE-V2", inputs=author.outputs, depends_on=[author.task_id],
                       api_contracts=next_contract)
    checker = task_with(checker, depends_on=[second.task_id])
    plan = plan_with(plan, [author, second, checker])
    assert _available_api_contracts(plan, checker, _input_tasks(plan, checker)) == next_contract


@pytest.mark.parametrize("kind", ["implement", "doc"])
def test_document_authors_and_verifiers_are_told_to_use_text_and_report_metadata_only(bundle, kind):
    task = task_with(bundle.plan.tasks[0], type=kind, write_scope=["docs/README.md"] if kind == "implement"
                     else ["reports/docs"], outputs=["docs/README.md"])
    manifest = _task_manifest(bundle.requirement, bundle.design, task, [])
    assert "DOCUMENTATION STAGE" in manifest["prompt"]
    assert "Do not construct business services" in manifest["prompt"]

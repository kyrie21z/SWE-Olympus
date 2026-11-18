"""Host metadata records do not acquire business-method keys to satisfy checks."""

from types import MappingProxyType

import pytest

from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError
from olympus.agents.runtime.providers import ProviderStepResult
from olympus.execution.source_metadata import build_source_metadata
from olympus.workflows.acceptance import generate_acceptance, validate_check
from olympus.workflows.service import WorkflowRunner

from test_acceptance_corrections import plan_with, task_with


SOURCE_PATH = "interfaces/ISearchAPI.py"
SETUP = f"facts = source_metadata[{SOURCE_PATH!r}]\n"
BAD_CHECK = SETUP + "assert 'search' in facts['signatures']['ISearchAPI.search']\n"
GOOD_CHECK = (
    SETUP + "assert 'ISearchAPI.search' in facts['public_symbols']\n"
    "assert 'ISearchAPI.search' in facts['signatures']\n"
    "signature = facts['signatures']['ISearchAPI.search']\n"
    "assert 'parameters' in signature and 'return_annotation' in signature\n"
    "assert signature['parameters'][1]['name'] == 'keyword'\n"
    "assert signature['return_annotation'] == 'list'\n"
)


def source_facts():
    def freeze(value):
        if isinstance(value, dict):
            return MappingProxyType({key: freeze(item) for key, item in value.items()})
        if isinstance(value, list):
            return tuple(freeze(item) for item in value)
        return value

    return freeze(build_source_metadata({SOURCE_PATH: (
        "class ISearchAPI:\n    def search(self, keyword: str) -> list:\n        ...\n"
    ), "settings.json": '{"signatures":{"search":true}}'}))


def test_real_signature_membership_failure_is_rejected_before_freezing():
    metadata = source_facts()
    with pytest.raises(AssertionError):
        exec(compile(BAD_CHECK, "<invalid-metadata-check>", "exec"), {"source_metadata": metadata})
    with pytest.raises(OutputSchemaError, match="incompatible source metadata") as failure:
        validate_check(BAD_CHECK, [SOURCE_PATH])
    assert "parameters" in failure.value.hint and "return_annotation" in failure.value.hint
    assert "qualified_name in facts['signatures']" in failure.value.hint
    assert set(metadata[SOURCE_PATH]["signatures"]["ISearchAPI.search"]) == {
        "parameters", "return_annotation",
    }


def test_legal_record_keys_open_symbol_maps_and_json_payloads_keep_working():
    source = GOOD_CHECK + (
        "assert 'not_a_declared_symbol' not in facts['signatures']\n"
        "assert source_metadata['settings.json']['data']['signatures']['search'] is True\n"
    )
    validate_check(source, [SOURCE_PATH])
    exec(compile(source, "<valid-metadata-check>", "exec"), {"source_metadata": source_facts()})


class MetadataCorrectionProvider(MockProvider):
    mode = "real_model"

    def __init__(self):
        super().__init__(fixtures={})
        self.prompts = []

    def steps(self, request):
        return [{}]

    def invoke(self, request, step, *, step_index, prompt=None):
        self.prompts.append(prompt)
        task = request.input_manifest["tasks"][0]
        source = BAD_CHECK if len(self.prompts) == 1 else GOOD_CHECK
        return ProviderStepResult(kind="model", output={"tasks": [{
            "task_id": task["task_id"], "checks": {task["checks"][0]: source},
        }]})


def test_wrong_signature_membership_gets_one_format_correction(bundle):
    task = task_with(bundle.plan.tasks[0], write_scope=[SOURCE_PATH], outputs=[SOURCE_PATH],
                     checks=["Verify declared search signature"])
    provider = MetadataCorrectionProvider()
    runner = WorkflowRunner(AgentRunner(provider), "RUN-METADATA-SCHEMA", 5)
    result = generate_acceptance(runner, bundle.requirement, bundle.design, plan_with(bundle.plan, [task]))
    assert len(provider.prompts) == 2
    assert "return_annotation" in provider.prompts[1]["format_correction"]
    assert result[task.task_id]["checks"][task.checks[0]] == GOOD_CHECK
    assert runner.ledger.total_calls_accounted("RUN-METADATA-SCHEMA") == 2

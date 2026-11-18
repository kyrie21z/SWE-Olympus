"""Implementation policy failures can be corrected before the adapter runs."""

import pytest

from olympus.agents.hephaestus import HephaestusImplementer
from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError, RunnerPermissionError
from olympus.contracts.models import Task, fingerprint
from olympus.workflows.service import WorkflowRunner


LEGAL_SOURCE = '''from threading import Lock

def copy_records(records):
    return dict(records)

class Service:
    def __init__(self):
        self.records = {}
        self.lock = Lock()

    def read(self):
        with self.lock:
            return copy_records(self.records)
'''
PRIVATE_SOURCE = '''class Service:
    def __init__(self):
        self._records = {}
'''


class ImplementationProvider(MockProvider):
    mode = "real_model"

    def __init__(self, changes):
        super().__init__(fixtures={"Hephaestus:implement": {"output": {"changes": changes[0]}}})
        self.changes, self.prompts = changes, []

    def invoke(self, request, step, **kwargs):
        self.prompts.append(kwargs["prompt"])
        changes = self.changes[min(len(self.prompts) - 1, len(self.changes) - 1)]
        return super().invoke(request, {**step, "output": {"changes": changes}}, **kwargs)


def propose(bundle, provider):
    runner = WorkflowRunner(AgentRunner(provider), "implementation-policy-test", 10)
    result = HephaestusImplementer(runner).propose(
        bundle.plan.tasks[0], [bundle.design.ref()], run_id="implementation-policy-test", generation=1,
    )
    return result, runner


def change(source, path="packages/t01/contract.py"):
    return [{"path": path, "content": source}]


def test_private_storage_gets_one_actionable_model_correction_before_execution(bundle):
    provider = ImplementationProvider([change(PRIVATE_SOURCE), change(LEGAL_SOURCE)])
    result, runner = propose(bundle, provider)
    assert result.structured_output["changes"] == change(LEGAL_SOURCE)
    assert len(provider.prompts) == 2
    hint = provider.prompts[1]["format_correction"]
    assert "self.records" in hint and "模块级" in hint and "self._records" in hint
    assert "self._x" in provider.prompts[0]["prompt"]
    assert "typing, re, unittest" in provider.prompts[0]["prompt"]
    assert runner.ledger.total_calls_accounted("implementation-policy-test") == 2


@pytest.mark.parametrize(("source", "hint_fragment"), [
    ("def invalid(:\n    pass\n", "第 1 行"),
    ("import typing\n", "typing"),
    ("import re\n", "ASCII"),
    ("class Service:\n    def run(self):\n        return self._helper()\n", "辅助函数"),
    ("value = getattr(object(), 'name')\n", "getattr"),
])
def test_rejected_python_receives_specific_correction_and_can_recover(bundle, source, hint_fragment):
    provider = ImplementationProvider([change(source), change(LEGAL_SOURCE)])
    result, _ = propose(bundle, provider)
    assert result.structured_output["changes"] == change(LEGAL_SOURCE)
    assert len(provider.prompts) == 2
    assert hint_fragment in provider.prompts[1]["format_correction"]


def test_legal_public_storage_and_module_helpers_need_no_correction(bundle):
    provider = ImplementationProvider([change(LEGAL_SOURCE)])
    result, _ = propose(bundle, provider)
    assert result.structured_output["changes"] == change(LEGAL_SOURCE)
    assert len(provider.prompts) == 1


def test_persistent_policy_failure_stops_after_existing_single_correction(bundle):
    provider = ImplementationProvider([change(PRIVATE_SOURCE)])
    with pytest.raises(OutputSchemaError, match="local execution policy"):
        propose(bundle, provider)
    assert len(provider.prompts) == 2


@pytest.mark.parametrize(("path", "content"), [
    ("README.md", "# 使用说明\n```python\nself._example\n```\n"),
    ("contract.json", '{"note":"__class__ is plain JSON text","valid":true}'),
])
def test_non_python_documents_are_preserved_without_python_policy_parsing(bundle, path, content):
    data = bundle.plan.tasks[0].semantic_content() | {"write_scope": [path]}
    task = Task(**data, version=1, content_hash=fingerprint(data))
    provider = ImplementationProvider([change(content, path)])
    result = HephaestusImplementer(AgentRunner(provider)).propose(
        task, [bundle.design.ref()], run_id="document-policy-test", generation=1,
    )
    assert result.structured_output["changes"] == change(content, path)
    assert len(provider.prompts) == 1


def test_path_permission_audit_still_rejects_outside_writes_without_correction(bundle):
    provider = ImplementationProvider([change(PRIVATE_SOURCE, "../outside.py"), change(LEGAL_SOURCE)])
    with pytest.raises(RunnerPermissionError):
        propose(bundle, provider)
    assert len(provider.prompts) == 1

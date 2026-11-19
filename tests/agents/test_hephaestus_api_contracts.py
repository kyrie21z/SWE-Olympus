"""Declared project API names are corrected before implementation execution."""

import pytest

from olympus.agents.hephaestus import HephaestusImplementer
from olympus.agents.runtime import AgentRunner, MockProvider, RunnerPermissionError
from olympus.contracts.models import Task, fingerprint
from olympus.workflows.service import WorkflowRunner


SOURCE_PATH = "modules/DocumentStoreModule.py"
API_CONTRACTS = {
    SOURCE_PATH: {"DocumentStoreModule": ["save_document", "list_titles", "get_version"]},
}
LEGAL_SOURCE = '''from threading import Lock

class DocumentStoreModule:
    def __init__(self):
        self.records = {}
        self.lock = Lock()

    def save_document(self, title, content):
        with self.lock:
            entries = self.records.get(title, ()) + (content,)
            self.records[title] = entries
            return len(entries)

    def list_titles(self):
        return list(self.records)

    def get_version(self, title, version_id):
        return self.records[title][version_id - 1]
'''


class ImplementationProvider(MockProvider):
    mode = "real_model"

    def __init__(self, changes):
        super().__init__(fixtures={"Hephaestus:implement": {"output": {"changes": changes[0]}}})
        self.changes = changes
        self.prompts = []

    def invoke(self, request, step, **kwargs):
        self.prompts.append(kwargs["prompt"])
        changes = self.changes[min(len(self.prompts) - 1, len(self.changes) - 1)]
        return super().invoke(request, {**step, "output": {"changes": changes}}, **kwargs)


def change(content, path=SOURCE_PATH):
    return [{"path": path, "content": content}]


def task_for(bundle, *, contracts=API_CONTRACTS, scope=SOURCE_PATH):
    data = bundle.plan.tasks[0].semantic_content() | {"type": "implement", "write_scope": [scope]}
    if contracts is not None:
        data["api_contracts"] = contracts
    else:
        data.pop("api_contracts", None)
    return Task(**data, version=1, content_hash=fingerprint(data))


def propose(bundle, provider, *, contracts=API_CONTRACTS, scope=SOURCE_PATH):
    runner = WorkflowRunner(AgentRunner(provider), "implementation-api-contract-test", 10)
    result = HephaestusImplementer(runner).propose(
        task_for(bundle, contracts=contracts, scope=scope), [bundle.design.ref()],
        run_id="implementation-api-contract-test", generation=1,
    )
    return result, runner


@pytest.mark.parametrize(("source", "canonical_name"), [
    (LEGAL_SOURCE.replace("class DocumentStoreModule:", "class DocumentStore:"), "DocumentStoreModule"),
    (LEGAL_SOURCE.replace("def save_document(", "def save("), "DocumentStoreModule.save_document"),
])
def test_wrong_class_or_missing_method_gets_one_canonical_correction(bundle, source, canonical_name):
    provider = ImplementationProvider([change(source), change(LEGAL_SOURCE)])
    result, runner = propose(bundle, provider)

    assert result.structured_output["changes"] == change(LEGAL_SOURCE)
    assert len(provider.prompts) == 2
    correction = provider.prompts[1]["format_correction"]
    assert SOURCE_PATH in correction
    assert canonical_name in correction
    assert provider.prompts[0]["task"]["api_contracts"] == API_CONTRACTS
    assert "task.api_contracts exactly" in provider.prompts[0]["prompt"]
    assert "aliases" in provider.prompts[0]["prompt"]
    assert runner.ledger.total_calls_accounted("implementation-api-contract-test") == 2


def test_canonical_source_and_legacy_lock_source_need_no_correction(bundle):
    for contracts in (API_CONTRACTS, None):
        provider = ImplementationProvider([change(LEGAL_SOURCE)])
        result, runner = propose(bundle, provider, contracts=contracts)

        assert result.structured_output["changes"] == change(LEGAL_SOURCE)
        assert len(provider.prompts) == 1
        assert "format_correction" not in provider.prompts[0]
        assert runner.ledger.total_calls_accounted("implementation-api-contract-test") == 1
    legacy = task_for(bundle, contracts=None)
    assert legacy.api_contracts is None
    assert "api_contracts" not in legacy.semantic_content()


def test_outside_scope_is_rejected_before_api_correction(bundle):
    wrong_class = LEGAL_SOURCE.replace("class DocumentStoreModule:", "class DocumentStore:")
    provider = ImplementationProvider([
        change(wrong_class) + change(LEGAL_SOURCE, "modules/Unauthorized.py"), change(LEGAL_SOURCE),
    ])

    with pytest.raises(RunnerPermissionError):
        propose(bundle, provider)
    assert len(provider.prompts) == 1


def test_readme_is_preserved_without_python_or_api_parsing(bundle):
    content = "# Usage\n```python\nclass DocumentStore:\n    self._example = '__class__'\n```\n"
    provider = ImplementationProvider([change(content, "docs/README.md")])

    result, _ = propose(bundle, provider, contracts=None, scope="docs/README.md")

    assert result.structured_output["changes"] == change(content, "docs/README.md")
    assert len(provider.prompts) == 1

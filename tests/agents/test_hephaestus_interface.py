"""Interface declarations obey explicit static contracts before execution starts."""

import pytest

from olympus.agents.hephaestus import HephaestusImplementer
from olympus.agents.runtime import AgentRunner, OutputSchemaError
from olympus.contracts.models import Task, fingerprint
from olympus.workflows.service import WorkflowRunner

from test_hephaestus_policy import ImplementationProvider, LEGAL_SOURCE, change


DECLARATIONS = '''class InventoryService:
    """FR-011: success ok/data or failure ok/error/code."""
    def create_sku(self, sku: str, initial: int) -> dict:
        """Return ok/data on success or ok/error/code on failure."""
        pass
    def reserve(self, order_id: str, sku: str, quantity: int) -> dict:
        """Return ok/data on success or ok/error/code on failure."""
        pass
    def confirm(self, order_id: str) -> dict:
        """Return ok/data on success or ok/error/code on failure."""
        pass
    def cancel(self, order_id: str) -> dict:
        """Return ok/data on success or ok/error/code on failure."""
        pass
    def get_sku(self, sku: str) -> dict:
        """Return ok/data on success or ok/error/code on failure."""
        pass
    def get_order(self, order_id: str) -> dict:
        """Return ok/data on success or ok/error/code on failure."""
        pass
'''
RUNTIME_INTERFACE = "from threading import Lock\n" + DECLARATIONS.replace(
    "class InventoryService:\n", "class InventoryService:\n    def __init__(self):\n        self.records = {}\n        self.lock = Lock()\n",
)


def task_with(bundle, *, kind="interface", check="Source metadata import_roots is empty; use built-in annotations"):
    data = bundle.plan.tasks[0].semantic_content() | {
        "type": kind, "goal": "Declare the six-method InventoryService static interface", "checks": [check],
    }
    return Task(**data, version=1, content_hash=fingerprint(data))


def propose(bundle, task, provider):
    runner = WorkflowRunner(AgentRunner(provider), "interface-contract-test", 10)
    result = HephaestusImplementer(runner).propose(task, [bundle.design.ref()],
        run_id="interface-contract-test", generation=1)
    return result, runner


def test_import_free_interface_lock_source_gets_exactly_one_declaration_correction(bundle):
    provider = ImplementationProvider([change(RUNTIME_INTERFACE), change(DECLARATIONS)])
    result, runner = propose(bundle, task_with(bundle), provider)
    assert result.structured_output["changes"] == change(DECLARATIONS)
    assert len(provider.prompts) == runner.ledger.total_calls_accounted("interface-contract-test") == 2
    prompt = provider.prompts[0]["prompt"]
    assert "No imports" in prompt and "docstrings" in prompt and "built-in type annotations" in prompt
    assert "constructors, storage/state, locks, validation helpers" in prompt
    assert "later implement tasks" in prompt
    hint = provider.prompts[1]["format_correction"]
    assert "import_roots" in hint and "threading.Lock" in hint and "pass" in hint
    assert "ok/error/code" in result.structured_output["changes"][0]["content"]


def test_valid_interface_declarations_are_preserved_without_extra_call(bundle):
    provider = ImplementationProvider([change(DECLARATIONS)])
    result, _ = propose(bundle, task_with(bundle), provider)
    assert result.structured_output["changes"] == change(DECLARATIONS)
    assert len(provider.prompts) == 1


def test_implementation_lock_and_state_remain_legal_even_with_generic_source_checks(bundle):
    provider = ImplementationProvider([change(LEGAL_SOURCE)])
    result, _ = propose(bundle, task_with(bundle, kind="implement", check="Runtime concurrency uses threading.Lock"), provider)
    assert result.structured_output["changes"] == change(LEGAL_SOURCE)
    assert len(provider.prompts) == 1


def test_generic_interface_stdlib_import_contract_keeps_existing_compatibility(bundle):
    provider = ImplementationProvider([change(LEGAL_SOURCE)])
    result, _ = propose(bundle, task_with(bundle, check="No third-party imports used"), provider)
    assert result.structured_output["changes"] == change(LEGAL_SOURCE)
    assert len(provider.prompts) == 1


def test_persistent_empty_import_violation_stops_after_single_correction(bundle):
    provider = ImplementationProvider([change(RUNTIME_INTERFACE)])
    with pytest.raises(OutputSchemaError, match="empty-import"):
        propose(bundle, task_with(bundle), provider)
    assert len(provider.prompts) == 2

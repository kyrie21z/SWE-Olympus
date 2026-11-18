"""Correct the actual script violation once, with bounded program-owned feedback."""

import pytest

from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError
from olympus.agents.runtime.providers import ProviderStepResult
from olympus.contracts.models import Plan, Task, fingerprint
from olympus.workflows.acceptance import _input_tasks, generate_acceptance, validate_check
from olympus.workflows.service import WorkflowRunner

DIRECT_CHECK = (
    "from inventory.service import InventoryService\n"
    "service = InventoryService()\n"
    "assert service.get_sku('missing') == {'ok': False, 'error': {'code': 'SKU_NOT_FOUND'}}\n"
)


def task_with(task, **changes):
    content = {**task.semantic_content(), **changes}
    return Task(**content, version=task.version + 1, content_hash=fingerprint(content))


def plan_with(plan, tasks):
    content = {"tasks": [{"task_id": task.task_id, "version": task.version, "content_hash": task.content_hash}
                         for task in tasks]}
    return Plan.model_validate({**plan.model_dump(mode="json"), "tasks": tasks,
                                "content": content, "content_hash": fingerprint(content)})


class CorrectionProvider(MockProvider):
    mode = "real_model"

    def __init__(self, initial):
        super().__init__(fixtures={})
        self.initial, self.prompts = initial, []

    def steps(self, request):
        return [{}]

    def invoke(self, request, step, *, step_index, prompt=None):
        self.prompts.append(prompt)
        task = request.input_manifest["tasks"][0]
        output = self.initial(task) if len(self.prompts) == 1 else {"tasks": [{
            "task_id": task["task_id"], "checks": {check: DIRECT_CHECK for check in task["checks"]},
        }]}
        return ProviderStepResult(kind="model", output=output, provider_request_id=f"fixture-{len(self.prompts)}")


def script_output(script):
    return lambda task: {"tasks": [{"task_id": task["task_id"],
                                    "checks": {check: script for check in task["checks"]}}]}


@pytest.mark.parametrize(("initial", "expected"), [
    (script_output("from inventory.service import InventoryService\n"
                   "assert getattr(InventoryService, 'get_sku')\n"), "getattr"),
    (script_output("import ast\nassert 1 == 1\n"), "ast"),
    (script_output("from inventory.service import InventoryService\n"
                   "result = InventoryService().get_sku('missing')\nassert result.__class__ is dict\n"),
     "isinstance(result, dict)"),
    (script_output("if True assert True\n"), "语法错误"),
    (lambda task: {"content": "model-only-private-output"}, "tasks"),
    (lambda task: {"tasks": [{"task_id": task["task_id"]}]}, "task_id 和 checks"),
    (lambda task: {"tasks": [{"task_id": "wrong-task", "checks": {}}]}, "T01"),
    (lambda task: {"tasks": [{"task_id": task["task_id"], "checks": []}]}, "JSON 对象"),
])
def test_one_format_correction_receives_concrete_hint_and_accepts_public_api_check(bundle, initial, expected):
    task = task_with(bundle.plan.tasks[0], write_scope=["inventory/service.py"])
    plan = plan_with(bundle.plan, [task])
    provider = CorrectionProvider(initial)
    runner = WorkflowRunner(AgentRunner(provider), "RUN-CORRECTION", 10)
    result = generate_acceptance(runner, bundle.requirement, bundle.design, plan)
    assert len(provider.prompts) == 2
    assert expected in provider.prompts[1]["format_correction"]
    assert "model-only-private-output" not in provider.prompts[1]["format_correction"]
    assert result[task.task_id]["checks"] == {check: DIRECT_CHECK for check in task.checks}
    prompt = provider.prompts[0]
    assert "inventory" in prompt["allowed_import_roots"]
    assert "getattr, hasattr" in prompt["prompt"] and "directly call" in prompt["prompt"]


def test_all_invalid_check_scripts_contribute_feedback_to_the_same_correction(bundle):
    task = task_with(bundle.plan.tasks[0], write_scope=["inventory/service.py"], checks=["check:api", "check:syntax"])
    plan = plan_with(bundle.plan, [task])
    provider = CorrectionProvider(lambda row: {"tasks": [{"task_id": row["task_id"], "checks": {
        "check:api": "assert getattr(object, 'x')\n", "check:syntax": "if True assert True\n",
    }}]})
    runner = WorkflowRunner(AgentRunner(provider), "RUN-ALL-CORRECTIONS", 10)
    result = generate_acceptance(runner, bundle.requirement, bundle.design, plan)
    hint = provider.prompts[1]["format_correction"]
    assert len(provider.prompts) == 2
    assert all(text in hint for text in ("check:api", "getattr", "check:syntax", "语法错误"))
    assert set(result[task.task_id]["checks"]) == set(task.checks)


@pytest.mark.parametrize("source", [
    "import unknown_private_model_text" + "x" * 130 + "\nassert 1 == 1\n",
    "assert __unknown_private_model_text\n",
    "assert 'unknown_private_model_text__'\n",
])
def test_validation_hints_do_not_echo_model_authored_identifiers_or_strings(source):
    with pytest.raises(OutputSchemaError) as failure:
        validate_check(source, ["inventory/service.py"])
    assert failure.value.hint
    assert "unknown_private_model_text" not in failure.value.hint


@pytest.mark.parametrize(("expression", "name", "alternative"), [
    ("result.__class__", "__class__", "isinstance(result, dict)"),
    ("service.__dict__", "__dict__", "公开方法"),
    ("service.__annotations__", "__annotations__", "实际参数校验"),
    ("service._internal_state", "_internal_state", "公开操作"),
])
def test_private_attribute_feedback_identifies_token_and_safe_public_alternative(expression, name, alternative):
    with pytest.raises(OutputSchemaError) as failure:
        validate_check(f"assert {expression}\n", ["inventory/service.py"])
    assert name in failure.value.hint and alternative in failure.value.hint


def test_standard_public_unittest_helpers_remain_allowed():
    validate_check(
        "import unittest\nfrom inventory.service import InventoryService\n"
        "class Checks(unittest.TestCase):\n"
        "    def setUp(self):\n        self.service = InventoryService()\n"
        "    def test_missing(self):\n"
        "        result = self.service.get_sku('missing')\n"
        "        self.assertIsInstance(result, dict)\n"
        "        self.assertEqual(result['error']['code'], 'SKU_NOT_FOUND')\n"
        "        with self.assertRaises(TypeError):\n            self.service.get_sku()\n"
        "suite = unittest.TestLoader().loadTestsFromTestCase(Checks)\n"
        "runner = unittest.TextTestRunner()\nassert runner.run(suite).wasSuccessful()\n",
        ["inventory/service.py"],
    )


def test_documentation_and_import_checks_read_trusted_metadata_without_private_introspection():
    validate_check(
        "assert 'FR-011' in source_metadata['inventory/service.py']['docstrings']['InventoryService.get_sku']\n"
        "assert 'InventoryService' in source_metadata['inventory/service.py']['public_symbols']\n"
        "assert all(name in {'math', 'threading'} for name in "
        "source_metadata['inventory/service.py']['import_roots'])\n"
        "assert 'python' in source_metadata['README.md']['text']\n",
        ["inventory/service.py"],
    )


@pytest.mark.parametrize("source", [
    "assert InventoryService.get_sku.__doc__\n",
    "InventoryService.get_sku.__doc__ = 'changed'\nassert 1 == 1\n",
    "del InventoryService.get_sku.__doc__\nassert 1 == 1\n",
    "assert _internal.__doc__\n",
    "assert InventoryService._private.__doc__\n",
    "assert InventoryService().__doc__\n",
    "assert InventoryService.get_sku.__doc__.__class__ is str\n",
    "assert InventoryService.__dict__\n",
    "assert InventoryService.get_sku.__globals__\n",
])
def test_documentation_metadata_does_not_allow_direct_private_introspection(source):
    with pytest.raises(OutputSchemaError):
        validate_check(source, ["inventory/service.py"])


@pytest.mark.parametrize("source", [
    "source_metadata = {}\nassert 1 == 1\n",
    "del source_metadata\nassert 1 == 1\n",
    "source_metadata['x.py'] = {}\nassert 1 == 1\n",
    "del source_metadata['x.py']['docstrings']\nassert 1 == 1\n",
    "source_metadata['x.py']['docstrings'].update({'method': 'fake'})\nassert 1 == 1\n",
    "def helper(source_metadata):\n    assert 1 == 1\n",
    "def source_metadata():\n    assert 1 == 1\n",
    "import math as source_metadata\nassert 1 == 1\n",
    "try:\n    assert 1 == 1\nexcept Exception as source_metadata:\n    pass\n",
    "match {}:\n    case source_metadata:\n        assert 1 == 1\n",
])
def test_host_source_metadata_binding_and_contents_cannot_be_modified(source):
    with pytest.raises(OutputSchemaError) as failure:
        validate_check(source, ["inventory/service.py"])
    assert "只读证据" in failure.value.hint


@pytest.mark.parametrize(("import_line", "base"), [
    ("import unittest", "unittest.TestCase"),
    ("import unittest as ut", "ut.TestCase"),
    ("from unittest import TestCase as Case", "Case"),
])
def test_direct_single_underscore_helper_on_stable_local_test_receiver_is_allowed(import_line, base):
    validate_check(
        f"{import_line}\nclass Checks({base}):\n"
        "    def _check_invariant(self, result):\n"
        "        self.assertIsInstance(result, dict)\n"
        "        self.assertEqual(result['initial'], result['available'] + result['reserved'])\n"
        "    def test_inventory(self):\n"
        "        result = {'initial': 3, 'available': 2, 'reserved': 1}\n"
        "        self._check_invariant(result)\n",
        ["inventory/service.py"],
    )


@pytest.mark.parametrize("test_body", [
    "service._check_invariant(result)",
    "self.service._check_invariant(result)",
    "Checks._check_invariant(self, result)",
    "callback = self._check_invariant\n        callback(result)",
    "self = service\n        self._check_invariant(result)",
    "for self in [service]:\n            self._check_invariant(result)",
    "self._check_invariant = replacement\n        self._check_invariant(result)",
    "del self._check_invariant",
    "assert self._private_state",
    "def nested():\n            self._check_invariant(result)\n        nested()",
])
def test_local_helper_name_does_not_allow_external_unbound_rebound_or_private_access(test_body):
    with pytest.raises(OutputSchemaError):
        validate_check(
            "import unittest\nclass Checks(unittest.TestCase):\n"
            "    def _check_invariant(self, result):\n        assert isinstance(result, dict)\n"
            f"    def test_inventory(self):\n        {test_body}\n",
            ["inventory/service.py"],
        )


@pytest.mark.parametrize("helper_body", [
    "assert self.__dict__",
    "assert service._check_invariant(result)",
    "assert getattr(service, 'state')",
    "import os\n        assert os.name",
])
def test_local_helper_body_is_still_fully_validated(helper_body):
    with pytest.raises(OutputSchemaError):
        validate_check(
            "import unittest\nclass Checks(unittest.TestCase):\n"
            f"    def _check_invariant(self, result):\n        {helper_body}\n"
            "    def test_inventory(self):\n        self._check_invariant({})\n",
            ["inventory/service.py"],
        )


@pytest.mark.parametrize(("base", "helper_name", "decorator"), [
    ("unittest.TestCase", "__helper", ""),
    ("object", "_check_invariant", ""),
    ("unittest.TestCase", "_check_invariant", "    @staticmethod\n"),
])
def test_dunder_helpers_non_test_classes_and_static_helpers_receive_no_exception(base, helper_name, decorator):
    with pytest.raises(OutputSchemaError):
        validate_check(
            f"import unittest\nclass Checks({base}):\n{decorator}"
            f"    def {helper_name}(self, result):\n        assert isinstance(result, dict)\n"
            f"    def test_inventory(self):\n        self.{helper_name}({{}})\n",
            ["inventory/service.py"],
        )


def test_same_file_inputs_select_closest_ancestor_without_current_or_future_tasks(bundle):
    base = bundle.plan.tasks[0]
    specs = [
        ("INTERFACE", [], [], ["inventory_service.py"]),
        ("SKU", ["INTERFACE"], ["inventory_service.py"], ["inventory_service.py"]),
        ("RESERVE", ["SKU"], ["inventory_service.py"], ["inventory_service.py"]),
        ("READ-SKU", ["SKU"], ["inventory_service.py"], ["report:sku"]),
        ("CONFIRM", ["RESERVE"], ["inventory_service.py"], ["inventory_service.py"]),
    ]
    tasks = [task_with(base, task_id=task_id, depends_on=deps, inputs=inputs, outputs=outputs)
             for task_id, deps, inputs, outputs in specs]
    plan = plan_with(bundle.plan, tasks)
    assert [task.task_id for task in _input_tasks(plan, tasks[1])] == ["INTERFACE"]
    assert [task.task_id for task in _input_tasks(plan, tasks[2])] == ["INTERFACE", "SKU"]
    assert [task.task_id for task in _input_tasks(plan, tasks[3])] == ["INTERFACE", "SKU"]
    assert [task.task_id for task in _input_tasks(plan, tasks[4])] == ["INTERFACE", "SKU", "RESERVE"]


def test_parallel_input_producers_fail_with_actionable_hint(bundle):
    base = bundle.plan.tasks[0]
    tasks = [task_with(base, task_id=task_id, depends_on=deps, inputs=inputs, outputs=outputs)
             for task_id, deps, inputs, outputs in [
                 ("LEFT", [], [], ["shared.py"]), ("RIGHT", [], [], ["shared.py"]),
                 ("READ", ["LEFT", "RIGHT"], ["shared.py"], ["report:shared"]),
             ]]
    with pytest.raises(OutputSchemaError, match="ambiguous parallel") as failure:
        _input_tasks(plan_with(bundle.plan, tasks), tasks[-1])
    assert "唯一 ID" in failure.value.hint


def test_future_input_is_rejected_but_external_document_input_is_preserved(bundle):
    base = bundle.plan.tasks[0]
    early = task_with(base, task_id="EARLY", depends_on=[], inputs=["later.py"], outputs=["early.py"])
    later = task_with(base, task_id="LATER", depends_on=["EARLY"], inputs=[], outputs=["later.py"])
    plan = plan_with(bundle.plan, [early, later])
    with pytest.raises(OutputSchemaError, match="dependency ancestor"):
        _input_tasks(plan, early)
    external = task_with(early, inputs=["requirement-document"])
    assert _input_tasks(plan_with(plan, [external, later]), external) == []

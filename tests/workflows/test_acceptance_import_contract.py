"""Import capabilities reflect runtime Python artifacts, not declared stub names."""

import pytest

from olympus.agents.runtime import OutputSchemaError
from olympus.contracts.models import Task, fingerprint
from olympus.execution.profile import LOCAL_EXECUTION_CAPABILITIES
from olympus.workflows.acceptance import _allowed_import_roots, _task_manifest, validate_check, validate_executable_plan


def test_only_python_modules_and_package_directories_supply_implementation_import_roots():
    roots = _allowed_import_roots([
        "inventory_service.py", "packages/inventory/service.py", "contracts",
        "inventory_service.pyi", "README.md", "report.txt", "settings.json", "types/stubs.pyi",
        "_private.py", "module-with-dashes.py",
    ])
    assert roots == {"unittest", "math", "threading", "concurrent", "inventory_service", "packages", "contracts"}


def test_stub_only_scope_does_not_advertise_a_runtime_importable_module():
    roots = _allowed_import_roots(["inventory_service.pyi"])
    assert "inventory_service.pyi" not in roots
    assert "inventory_service" not in roots
    with pytest.raises(OutputSchemaError) as failure:
        validate_check("from inventory_service import InventoryService\nassert callable(InventoryService)\n",
                       ["inventory_service.pyi"])
    assert "inventory_service" in failure.value.hint
    assert ".pyi" in failure.value.hint and "运行时模块" in failure.value.hint


def test_python_scope_allows_the_matching_module_without_stub_or_doc_roots():
    validate_check("from inventory_service import InventoryService\nassert callable(InventoryService)\n",
                   ["inventory_service.py", "README.md"])


@pytest.mark.parametrize(("source", "name", "line"), [
    ("# interface check\nimport typing\nassert 1 == 1\n", "typing", 2),
    ("# interface check\nfrom typing import Any\nassert 1 == 1\n", "typing", 2),
    ("import math, inspect\nassert 1 == 1\n", "inspect", 1),
    ("import ast\nassert 1 == 1\n", "ast", 1),
])
def test_rejected_import_feedback_identifies_the_exact_module_and_line(source, name, line):
    with pytest.raises(OutputSchemaError) as failure:
        validate_check(source, ["inventory_service.py"])
    hint = failure.value.hint
    assert f"第 {line} 行导入 {name}" in hint
    if name == "typing":
        assert "source_metadata" in hint
    assert "# interface check" not in hint


def test_comma_import_feedback_separates_rejected_module_from_allowed_modules():
    with pytest.raises(OutputSchemaError) as failure:
        validate_check("import math, typing\nassert math.sqrt(4) == 2\n", ["inventory_service.py"])
    detail = failure.value.hint.split("允许的导入根为", 1)[0]
    assert "导入 typing" in detail and "math" not in detail


def test_interface_signature_checks_use_metadata_without_typing_or_reflection():
    validate_check(
        "signatures = source_metadata['inventory_service.py']['signatures']\n"
        "parameters = signatures['InventoryService.reserve']['parameters']\n"
        "assert [parameter['name'] for parameter in parameters] == ['self', 'order_id', 'sku', 'quantity']\n"
        "assert all(parameter['kind'] == 'positional_or_keyword' for parameter in parameters)\n"
        "assert signatures['InventoryService.reserve']['return_annotation'] == 'dict'\n",
        ["inventory_service.py"],
    )


def changed_task(task, **changes):
    content = {**task.semantic_content(), **changes}
    return Task(**content, version=task.version + 1, content_hash=fingerprint(content))


def test_manifest_matches_execution_capabilities_and_real_ancestor_reports(bundle):
    author = changed_task(bundle.plan.tasks[0], write_scope=["inventory_service.py"])
    previous = changed_task(bundle.plan.tasks[5], write_scope=["reports/check-stock"])
    current = changed_task(bundle.plan.tasks[7], write_scope=["reports/documentation"])
    manifest = _task_manifest(bundle.requirement, bundle.design, current, [author, previous])
    assert manifest["execution_capabilities"] == LOCAL_EXECUTION_CAPABILITIES
    assert "inventory_service" in manifest["allowed_import_roots"]
    assert "reports" not in manifest["allowed_import_roots"]
    inputs = {row["task_id"]: row for row in manifest["available_inputs"]}
    assert inputs[previous.task_id]["physical_report_paths"] == ["reports/check-stock/check-report.json"]
    assert inputs[author.task_id]["physical_report_paths"] == []
    assert "data" in manifest["source_metadata_schema"]["file_fields"]
    assert "signatures" in manifest["source_metadata_schema"]["file_fields"]


def test_executable_plan_preflight_reports_all_capability_conflicts_before_acceptance(bundle):
    from test_acceptance_corrections import plan_with

    interface = changed_task(bundle.plan.tasks[0], write_scope=["inventory_service.pyi"],
                             outputs=["inventory_service.pyi"])
    check = changed_task(bundle.plan.tasks[5], write_scope=["test_inventory.py"],
                         outputs=["test_inventory.py"], inputs=[], depends_on=[])
    with pytest.raises(OutputSchemaError) as failure:
        validate_executable_plan(plan_with(bundle.plan, [interface, check]))
    hint = failure.value.hint
    assert "UNSUPPORTED_IMPLEMENTATION_FILE" in hint
    assert "READ_ONLY_SCOPE_REQUIRES_DIRECTORY" in hint
    assert "READ_ONLY_OUTPUT_HAS_NO_AUTHOR" in hint


@pytest.mark.parametrize("statement", [
    "import source_metadata", "import source_metadata as metadata",
    "from source_metadata import public_symbols", "from source_metadata import signatures",
])
def test_source_metadata_import_forms_explain_injected_global_and_exact_shape(statement):
    with pytest.raises(OutputSchemaError) as failure:
        validate_check(statement + "\nassert 1 + 1 == 2\n", ["inventory_service_interface.py"])
    hint = failure.value.hint
    assert "不是模块" in hint and "预注入" in hint and "删除 import" in hint
    assert 'source_metadata["inventory_service_interface.py"]' in hint
    assert "public_symbols" in hint and "parameters" in hint and "['name']" in hint


def test_source_metadata_shape_example_uses_actual_path_and_executes_without_import(bundle):
    task = changed_task(bundle.plan.tasks[0], write_scope=["inventory_service_interface.py"])
    manifest = _task_manifest(bundle.requirement, bundle.design, task, [])
    example = manifest["source_metadata_example"]
    assert example["file_path"] == "inventory_service_interface.py"
    validate_check(example["code"], ["inventory_service_interface.py"])
    metadata = {"inventory_service_interface.py": {
        "public_symbols": ("InventoryService.reserve",),
        "signatures": {"InventoryService.reserve": {"parameters": (
            {"name": "self", "kind": "positional_or_keyword", "annotation": None, "has_default": False},
            {"name": "sku", "kind": "positional_or_keyword", "annotation": "str", "has_default": False},
        ), "return_annotation": "dict"}},
        "docstrings": {"InventoryService.reserve": "FR-002 reserve semantics."},
    }}
    exec(compile(example["code"], "<source-metadata-shape>", "exec"), {"source_metadata": metadata})

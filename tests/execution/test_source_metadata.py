"""Frozen acceptance sees actual bounded source facts without reflection or file access."""

import hashlib
import json
import subprocess
import sys

import pytest

from olympus.execution.source_metadata import CHECK_DRIVER, MAX_SOURCE_FILE_BYTES, build_source_metadata

from test_local_executor import dispatch_and_wait, make_world


SOURCE = '''"""FR-011 inventory contract."""
import math
from threading import Lock

class InventoryService:
    """FR-011 service result schema."""
    def reserve(self):
        """FR-011 returns a result mapping."""
        return {"ok": True, "data": {}}

    def _internal(self):
        return None
'''


def test_source_facts_are_derived_without_importing_or_executing_code():
    files = {"src/inventory.py": SOURCE, "README.md": "中文运行说明\npython checks.py\n",
             "src/not_executed.py": "raise RuntimeError('must not execute')\n", "data.json": "{}"}
    facts = build_source_metadata(files)
    python = facts["src/inventory.py"]
    assert python["sha256"] == hashlib.sha256(SOURCE.encode()).hexdigest()
    assert python["kind"] == "python"
    assert python["import_roots"] == ["math", "threading"]
    assert python["public_symbols"] == ["InventoryService", "InventoryService.reserve"]
    assert python["docstrings"]["module"] == "FR-011 inventory contract."
    assert python["docstrings"]["InventoryService.reserve"] == "FR-011 returns a result mapping."
    assert python["signatures"]["InventoryService.reserve"] == {
        "parameters": [{"name": "self", "kind": "positional_or_keyword", "annotation": None, "has_default": False}],
        "return_annotation": None,
    }
    assert facts["README.md"]["text"] == files["README.md"]
    assert facts["data.json"]["kind"] == "json" and facts["data.json"]["data"] == {}
    assert "text" not in facts["data.json"]


def test_report_metadata_preserves_actual_parsed_evidence_and_hash():
    report = {"task": "T06", "executor": "Momus", "checks": [
        {"check_id": "check-boundary", "exit_code": 0, "output": "2 assertions passed"},
    ], "inputs": [{"object_id": "artifact:implementation", "version": 1, "content_hash": "a" * 64}]}
    text = json.dumps(report, ensure_ascii=False)
    facts = build_source_metadata({"reports/t06/check-report.json": text})["reports/t06/check-report.json"]
    assert facts["data"] == report
    assert facts["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    with pytest.raises(ValueError, match="valid JSON in reports/t06/check-report.json"):
        build_source_metadata({"reports/t06/check-report.json": "{broken"})


def test_signatures_describe_parameter_kinds_and_structural_annotations_without_evaluation():
    source = '''def danger():
    raise RuntimeError("annotations and defaults must never run")

class Result:
    ok: bool
    data: dict[str, object]
    _private: str

class InventoryService:
    def create_sku(self, sku: str, /, initial: int = danger(), *extras: str,
                   trace: bool, note: str = None, **options: object) -> Result:
        pass

    async def reserve(self, order_id: str, quantity: int) -> danger():
        pass
'''
    facts = build_source_metadata({"inventory.py": source})["inventory.py"]
    create = facts["signatures"]["InventoryService.create_sku"]
    assert create["parameters"] == [
        {"name": "self", "kind": "positional_only", "annotation": None, "has_default": False},
        {"name": "sku", "kind": "positional_only", "annotation": "str", "has_default": False},
        {"name": "initial", "kind": "positional_or_keyword", "annotation": "int", "has_default": True},
        {"name": "extras", "kind": "var_positional", "annotation": "str", "has_default": False},
        {"name": "trace", "kind": "keyword_only", "annotation": "bool", "has_default": False},
        {"name": "note", "kind": "keyword_only", "annotation": "str", "has_default": True},
        {"name": "options", "kind": "var_keyword", "annotation": "object", "has_default": False},
    ]
    assert create["return_annotation"] == "Result"
    assert facts["signatures"]["InventoryService.reserve"]["return_annotation"] == "danger()"
    assert facts["annotations"] == {"Result.ok": "bool", "Result.data": "dict[str, object]"}


@pytest.mark.parametrize("relative", ["../secret.py", "/absolute.py", "C:/secret.py", "./file.py", "src\\file.py"])
def test_source_metadata_rejects_unbounded_or_noncanonical_paths(relative):
    with pytest.raises(ValueError, match="relative paths"):
        build_source_metadata({relative: "pass\n"})


def test_source_metadata_rejects_over_budget_bytes_and_case_collisions():
    with pytest.raises(ValueError, match="size budget"):
        build_source_metadata({"large.txt": "字" * (MAX_SOURCE_FILE_BYTES // 3 + 1)})
    with pytest.raises(ValueError, match="relative paths"):
        build_source_metadata({"Src.py": "pass\n", "src.py": "pass\n"})


def test_source_metadata_enforces_total_budget_without_truncating_evidence(monkeypatch):
    monkeypatch.setattr("olympus.execution.source_metadata.MAX_SOURCE_BYTES", 10)
    with pytest.raises(ValueError, match="size budget"):
        build_source_metadata({"one.txt": "123456", "two.txt": "123456"})


def test_driver_injects_deeply_read_only_metadata_in_isolated_process(tmp_path):
    driver = tmp_path / "driver.py"
    metadata = tmp_path / "source_metadata.json"
    check = tmp_path / "check.py"
    driver.write_text(CHECK_DRIVER, encoding="utf-8")
    metadata.write_text(json.dumps(build_source_metadata({"src/inventory.py": SOURCE,
        "reports/t06/check-report.json": '{"task":"T06","checks":[{"exit_code":0}]}'})), encoding="utf-8")
    check.write_text('''entry = source_metadata["src/inventory.py"]
assert "FR-011" in entry["docstrings"]["InventoryService.reserve"]
for mapping, key in [(source_metadata, "extra"), (entry, "kind"), (entry["docstrings"], "module")]:
    try:
        mapping[key] = "tampered"
    except TypeError:
        pass
    else:
        raise AssertionError("metadata was writable")
assert entry["import_roots"] == ("math", "threading")
parameter = entry["signatures"]["InventoryService.reserve"]["parameters"][0]
try:
    parameter["name"] = "tampered"
except TypeError:
    pass
else:
    raise AssertionError("signature metadata was writable")
report = source_metadata["reports/t06/check-report.json"]["data"]
assert report["checks"][0]["exit_code"] == 0
try:
    report["checks"][0]["exit_code"] = 1
except TypeError:
    pass
else:
    raise AssertionError("report evidence was writable")
''', encoding="utf-8")
    result = subprocess.run([sys.executable, "-I", "-S", "-B", str(driver), str(tmp_path), str(check), str(metadata)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr


def test_executor_checks_actual_source_docstrings_and_imports_and_freezes_metadata(bundle, tmp_path):
    check = '''entry = source_metadata["packages/t01/contract.py"]
assert entry["kind"] == "python"
assert set(entry["import_roots"]) <= {"math", "threading"}
assert "InventoryService.reserve" in entry["public_symbols"]
assert "FR-011" in entry["docstrings"]["InventoryService.reserve"]
assert entry["signatures"]["InventoryService.reserve"]["parameters"][0]["name"] == "self"
from packages.t01.contract import InventoryService
assert InventoryService().reserve() == {"ok": True, "data": {}}
'''
    scheduler, adapter, service, _ = make_world(bundle, tmp_path, source=SOURCE, check=check)
    receipt = dispatch_and_wait(scheduler, adapter)
    assert receipt.state == "succeeded", receipt.error
    directory = adapter.root / receipt.attempt_id
    metadata = json.loads((directory / "checks/source_metadata.json").read_text(encoding="utf-8"))
    actual = (directory / "work/packages/t01/contract.py").read_bytes()
    assert metadata["packages/t01/contract.py"]["sha256"] == hashlib.sha256(actual).hexdigest()
    assert set(metadata) == {"packages/t01/contract.py"}
    assert service.collect("T01", receipt.attempt_id)[0] == "accepted"


def test_tampered_metadata_invalidates_actual_execution_evidence(bundle, tmp_path):
    scheduler, adapter, service, _ = make_world(bundle, tmp_path)
    receipt = dispatch_and_wait(scheduler, adapter)
    path = adapter.root / receipt.attempt_id / "checks/source_metadata.json"
    path.write_text("{}", encoding="utf-8")
    assert service.collect("T01", receipt.attempt_id)[0] == "rejected"

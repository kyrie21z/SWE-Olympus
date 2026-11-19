"""Machine-readable task ABI is semantic only when it was explicitly declared."""

from copy import deepcopy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from olympus.agents.daedalus import DaedalusPlanner
from olympus.agents.daedalus.planner import TaskProposal
from olympus.agents.runtime import OutputSchemaError
from olympus.contracts.models import Task, fingerprint


def declared_task(task, contracts, scopes=None):
    content = task.semantic_content() | {"api_contracts": contracts, "write_scope": scopes or ["modules/"]}
    return Task(**content, version=task.version + 1, content_hash=fingerprint(content))


def test_legacy_tasks_and_materialized_proposals_keep_exact_original_hashes(bundle):
    raw = json.loads((Path(__file__).resolve().parents[2] / "fixtures/contracts/valid.json").read_text(encoding="utf-8"))
    for original, task in zip(raw["plan"]["tasks"], bundle.plan.tasks, strict=True):
        semantic = {key: value for key, value in original.items() if key not in {"version", "content_hash", "schema_version"}}
        assert task.api_contracts is None and task.semantic_content() == semantic
        assert "api_contracts" not in task.model_dump(mode="json")
        assert fingerprint(semantic) == original["content_hash"] == task.content_hash
    # Older clients may send an explicit null; it has exactly the same meaning as omission.
    rows = [task.semantic_content() | {"api_contracts": None} for task in bundle.plan.tasks]
    result = DaedalusPlanner._materialize({"tasks": rows}, bundle.requirement, bundle.design, "real_model")
    assert [task.content_hash for task in result.tasks] == [
        task.content_hash for task in bundle.plan.tasks]
    assert result.content_hash == bundle.plan.content_hash


def test_declared_class_method_contract_changes_hash_and_is_materialized(bundle):
    contracts = {"modules/DocumentStoreModule.py": {"DocumentStoreModule": ["save_document", "get_document"],
                                                    "MissingDocument": []}}
    task = declared_task(bundle.plan.tasks[0], contracts)
    assert task.api_contracts == contracts and task.semantic_content()["api_contracts"] == contracts
    changed = deepcopy(contracts)
    changed["modules/DocumentStoreModule.py"]["DocumentStoreModule"].append("delete_document")
    assert declared_task(bundle.plan.tasks[0], changed).content_hash != task.content_hash
    plan = DaedalusPlanner._materialize({"tasks": [task.semantic_content()]},
                                      bundle.requirement, bundle.design, "real_model")
    assert plan.tasks[0].content_hash == task.content_hash
    assert plan.tasks[0].api_contracts == contracts
    assert "api_contracts" in DaedalusPlanner().build_prompt(bundle.requirement, bundle.design)


@pytest.mark.parametrize(("filename", "exports", "scopes"), [
    ("../Store.py", {"Store": ["save"]}, ["modules/"]),
    ("/Store.py", {"Store": ["save"]}, ["modules/"]),
    ("C:/Store.py", {"Store": ["save"]}, ["modules/"]),
    ("modules/../Store.py", {"Store": ["save"]}, ["modules/"]),
    ("modules\\Store.py", {"Store": ["save"]}, ["modules/"]),
    ("modules/Store.pyi", {"Store": ["save"]}, ["modules/"]),
    ("elsewhere/Store.py", {"Store": ["save"]}, ["modules/"]),
    ("modules/Store.py", {"_Store": ["save"]}, ["modules/"]),
    ("modules/Store.py", {"Store": ["_save"]}, ["modules/"]),
    ("modules/Store.py", {"Store": ["__init__"]}, ["modules/"]),
    ("modules/Store.py", {"Store": ["not-method"]}, ["modules/"]),
    ("modules/Store.py", {"class": ["save"]}, ["modules/"]),
    ("modules/Store.py", {"Store": ["save", "save"]}, ["modules/"]),
    ("modules/Store.py", {"Store": ["save"]}, ["../modules/"]),
], ids=["traversal", "absolute", "drive", "internal-traversal", "backslash", "stub", "outside-scope",
         "private-class", "private-method", "constructor", "invalid-identifier", "keyword", "duplicate", "unsafe-scope"])
def test_task_and_task_proposal_reject_invalid_api_declarations(bundle, filename, exports, scopes):
    contracts = {filename: exports}
    with pytest.raises(ValidationError, match="api_contracts"):
        declared_task(bundle.plan.tasks[0], contracts, scopes)
    row = bundle.plan.tasks[0].semantic_content() | {"api_contracts": contracts, "write_scope": scopes}
    with pytest.raises(ValidationError, match="api_contracts"):
        TaskProposal.model_validate(row)
    with pytest.raises(OutputSchemaError) as failure:
        DaedalusPlanner._materialize({"tasks": [row]}, bundle.requirement, bundle.design, "real_model")
    assert "tasks[0].api_contracts" in failure.value.hint


def test_exact_file_scope_and_nested_directory_scope_are_supported(bundle):
    contracts = {"modules/nested/Store.py": {"Store": ["save"]}}
    assert declared_task(bundle.plan.tasks[0], contracts, ["modules/nested/Store.py"])
    assert declared_task(bundle.plan.tasks[0], contracts, ["modules/"])

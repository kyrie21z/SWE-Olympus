"""API paths share output coordinates; corrections never widen task permissions."""

from copy import deepcopy

import pytest

from olympus.agents.daedalus import DaedalusPlanner
from olympus.agents.daedalus.planner import _restrict_root_scope
from olympus.agents.runtime import AgentRunner, OutputSchemaError
from olympus.contracts.models import fingerprint
from olympus.workflows.service import WorkflowRunner

from test_proposal_schema_feedback import ProposalCorrectionProvider, request_for


def test_planner_explains_owned_api_paths_and_exact_root_file_scopes(bundle):
    planner = DaedalusPlanner()
    prompt = planner.build_input_manifest(bundle.requirement, bundle.design)["prompt"]
    assert "前驱 API 通过 inputs/depends_on" in prompt
    assert "同一坐标" in prompt and "不能为 'Store.py'" in prompt
    assert "write_scope=['interfaces.py']" in prompt and "不要使用 '.'" in prompt
    description = planner.draft_schema()["properties"]["tasks"]["items"]["properties"]["api_contracts"]["description"]
    assert "workspace-relative" in description and "Ancestor APIs" in description


def test_scope_mismatch_receives_actionable_hint_and_one_explicit_correction(bundle):
    rows = [task.semantic_content() for task in bundle.plan.tasks]
    rows[0] |= {"write_scope": ["modules/Store.py"], "outputs": ["modules/Store.py"],
                "api_contracts": {"Store.py": {"Store": ["save"]}}}
    corrected = deepcopy(rows)
    corrected[0]["api_contracts"] = {"modules/Store.py": {"Store": ["save"]}}
    provider = ProposalCorrectionProvider({"tasks": rows}, {"tasks": corrected})
    runner = WorkflowRunner(AgentRunner(provider), "RUN-API-SCOPE", 5)
    runner.run(request_for(bundle), output_validator=lambda output: DaedalusPlanner._materialize(
        output, bundle.requirement, bundle.design, "real_model"))
    assert len(provider.prompts) == 2
    correction = provider.prompts[1]
    assert correction["previous_plan_output"] == {"tasks": rows}
    assert "tasks[0].api_contracts" in correction["format_correction"]
    assert "不能省略目录前缀" in correction["format_correction"]
    assert corrected[0]["write_scope"] == rows[0]["write_scope"]
    assert corrected[0]["checks"] == rows[0]["checks"]


def test_root_python_outputs_can_keep_exact_file_scopes_and_consumer_cannot_claim_them(bundle):
    row = bundle.plan.tasks[0].semantic_content() | {
        "write_scope": ["interfaces.py", "exceptions.py"], "outputs": ["interfaces.py", "exceptions.py"],
        "api_contracts": {"interfaces.py": {"Store": ["save"]}, "exceptions.py": {"MissingDocument": []}},
    }
    result = DaedalusPlanner._materialize({"tasks": [row]}, bundle.requirement, bundle.design, "real_model")
    assert result.tasks[0].api_contracts == row["api_contracts"]
    consumer = row | {"type": "test", "write_scope": ["reports"], "outputs": ["reports/check-report.json"]}
    with pytest.raises(OutputSchemaError) as failure:
        DaedalusPlanner._materialize({"tasks": [consumer]}, bundle.requirement, bundle.design, "real_model")
    assert "inputs/depends_on" in failure.value.hint
    assert consumer["api_contracts"] == row["api_contracts"]


def test_root_python_author_scope_is_restricted_before_hashing_with_audit_provenance(bundle):
    row = bundle.plan.tasks[0].semantic_content() | {
        "type": "interface", "executor": "Hephaestus", "write_scope": ["."],
        "outputs": ["interfaces.py", "exceptions.py"],
        "api_contracts": {"interfaces.py": {"Store": ["save"]}, "exceptions.py": {"MissingDocument": []}},
    }
    original = deepcopy(row)
    plan = DaedalusPlanner._materialize({"tasks": [row]}, bundle.requirement, bundle.design, "real_model")
    task = plan.tasks[0]
    assert task.write_scope == row["outputs"]
    assert task.semantic_content() == original | {"write_scope": original["outputs"]}
    assert task.content_hash == fingerprint(task.semantic_content())
    assert plan.content_hash == fingerprint(plan.content)
    assert row == original
    assert any(p.kind == "material" and p.source_id == "DAEDALUS-EXACT-OUTPUT-SCOPE" for p in plan.provenance)
    stable = deepcopy(task.semantic_content())
    assert _restrict_root_scope(stable) is False and stable == task.semantic_content()


@pytest.mark.parametrize("changes", [
    {"executor": "Momus"}, {"type": "test"}, {"write_scope": [".", "other"]},
    {"outputs": ["../Store.py"]}, {"outputs": ["/Store.py"]}, {"outputs": ["C:/Store.py"]},
    {"outputs": [".hidden/Store.py"]}, {"outputs": ["pkg/../Store.py"]},
    {"outputs": ["pkg\\Store.py"]}, {"outputs": ["pkg/"]}, {"outputs": ["Store.pyi"]},
    {"outputs": ["pkg\n/Store.py"]}, {"outputs": ["pkg /Store.py"]}, {"outputs": ["bad?/Store.py"]},
    {"outputs": ["Store.py", "README.md"]}, {"outputs": ["Store.py", "Store.py"]},
    {"api_contracts": None}, {"api_contracts": {}}, {"api_contracts": {"Other.py": {"Store": ["save"]}}},
    {"type": ["interface"]},
])
def test_ambiguous_or_unsafe_root_claim_is_never_automatically_restricted(changes):
    data = {"type": "implement", "executor": "Hephaestus", "write_scope": ["."],
            "outputs": ["Store.py"], "api_contracts": {"Store.py": {"Store": ["save"]}}} | changes
    if "outputs" in changes and "api_contracts" not in changes:
        data["api_contracts"] = {changes["outputs"][0]: {"Store": ["save"]}}
    before = deepcopy(data)
    assert _restrict_root_scope(data) is False and data == before

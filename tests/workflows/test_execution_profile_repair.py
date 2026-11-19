"""A saved semantic pass must not trap an unexecutable plan in script retries."""

import pytest

from olympus.agents.momus.planning import MomusPlanReviewer
from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError
from olympus.contracts.models import Plan, Task, fingerprint
from olympus.orchestrator.recovery import RuntimeRegistry
from olympus.storage.sqlite import Store
from olympus.workflows import WorkflowService
from olympus.workflows.service import LocalPlanner


def task_with(task, **changes):
    content = {**task.semantic_content(), **changes}
    return Task(**content, version=task.version + 1, content_hash=fingerprint(content))


def plan_with(plan, tasks, version):
    content = {"tasks": [{"task_id": t.task_id, "version": t.version, "content_hash": t.content_hash} for t in tasks]}
    return Plan.model_validate({**plan.model_dump(mode="json"), "tasks": tasks, "version": version,
                                "content": content, "content_hash": fingerprint(content)})


def legacy_host(tmp_path, bundle):
    tasks = list(bundle.plan.tasks)
    tasks[0] = task_with(tasks[0], write_scope=["service.pyi"])
    doc_task = next(index for index, task in enumerate(tasks) if task.type == "doc")
    tasks[doc_task] = task_with(tasks[doc_task], write_scope=["README.md"], outputs=["README.md"])
    bad = plan_with(bundle.plan, tasks, 2)
    legacy = MomusPlanReviewer(runner=AgentRunner(MockProvider(fixtures={
        "Momus:plan_review": {"output": {"verdict": "passed", "findings": []}},
    }))).review(bad, bundle.requirement, bundle.design).record
    assert legacy.review.verdict == "passed"
    store = Store(tmp_path / "state.sqlite3")
    host = WorkflowService(store, RuntimeRegistry(store), lambda ledger: AgentRunner(MockProvider(), ledger=ledger))
    doc = host.create(bundle.requirement.model_dump(mode="json"), allow_synthetic=True)
    doc.update(stage="error", design=bundle.design.model_dump(mode="json"), plan=bad.model_dump(mode="json"),
               plan_review=legacy.review.model_dump(mode="json"), plan_review_record=legacy.model_dump(mode="json"))
    doc["approvals"]["design"] = {"target": bundle.design.ref().model_dump(mode="json")}
    corrected = plan_with(bad, [task_with(old, **original.semantic_content())
                                for old, original in zip(bad.tasks, bundle.plan.tasks, strict=True)], 3)
    return host, doc, bad, corrected


def test_saved_pass_revalidates_all_profile_gaps_then_repairs_new_version(tmp_path, bundle, monkeypatch):
    host, doc, bad, corrected = legacy_host(tmp_path, bundle)
    observed, frozen = {}, []

    def repair(self, plan, findings, req, design, *, repair_context):
        observed.update(plan=plan, findings=findings, context=repair_context)
        return corrected

    monkeypatch.setattr(LocalPlanner, "revise_plan", repair)
    monkeypatch.setattr(host, "_generate_acceptance", lambda *args: frozen.append(args[-1].ref()))
    try:
        host._generate_plan(doc, "repair-saved-profile")
        assert observed["plan"].ref() == bad.ref()
        categories = {row["category"] for row in observed["context"]["active_findings"]}
        assert {"UNSUPPORTED_IMPLEMENTATION_FILE", "READ_ONLY_SCOPE_REQUIRES_DIRECTORY",
                "READ_ONLY_OUTPUT_HAS_NO_AUTHOR"} <= categories
        assert frozen == [corrected.ref()]
        assert doc["plan"]["version"] == 3
        assert doc["plan_review"]["verdict"] == "passed"
        assert doc["stage"] == "plan_pending_approval"
        assert "plan" not in doc["approvals"]
        history = doc["plan_review_history"]
        assert [r["review"]["verdict"] for r in history] == ["passed", "blocked", "passed"]
        assert "program:local-execution-profile" in history[-1]["submission"]["passed_checks"]
    finally:
        host.close()


def test_failed_profile_repair_preserves_blocked_baseline_and_never_generates_scripts(tmp_path, bundle, monkeypatch):
    host, doc, bad, _ = legacy_host(tmp_path, bundle)

    def fail(*args, **kwargs):
        raise OutputSchemaError("profile gaps remain", hint="Replace unsupported file types and report scopes.")

    monkeypatch.setattr(LocalPlanner, "revise_plan", fail)
    monkeypatch.setattr(host, "_generate_acceptance", lambda *args: pytest.fail("must not freeze invalid plan scripts"))
    try:
        with pytest.raises(OutputSchemaError):
            host._generate_plan(doc, "failed-profile-repair")
        assert doc["plan"] == bad.model_dump(mode="json")
        assert doc["plan_review"]["verdict"] == "blocked"
        assert doc["plan_review_record"]["blocking_finding_ids"]
        assert "plan" not in doc["approvals"]
    finally:
        host.close()


def test_rejected_candidate_is_checkpointed_and_used_as_the_next_repair_seed(tmp_path, bundle, monkeypatch):
    host, doc, bad, corrected = legacy_host(tmp_path, bundle)
    observed = {}

    def capture(self, plan, findings, req, design, *, repair_context):
        observed.update(repair_context)
        raise OutputSchemaError("stop after verifying candidate handoff")

    monkeypatch.setattr(LocalPlanner, "revise_plan", capture)
    try:
        host._record_plan_candidate(doc, corrected)
        assert doc["plan"] == bad.model_dump(mode="json")
        assert doc["plan_repair_candidate"]["status"] == "unreviewed"
        assert "plan" not in doc["approvals"]
        with pytest.raises(OutputSchemaError):
            host._generate_plan(doc, "continue-candidate")
        assert observed["candidate_tasks"] == [task.semantic_content() for task in corrected.tasks]
        assert observed["candidate_profile_issues"] == []
    finally:
        host.close()

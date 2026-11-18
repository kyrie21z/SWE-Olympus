"""Revalidate fixed program rules and send one complete repair backlog to the planner."""

import pytest

from olympus.agents.momus.planning import MomusPlanReviewer
from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError
from olympus.orchestrator.recovery import RuntimeRegistry
from olympus.reviews.common import CommonReviewer, ReviewFinding
from olympus.storage.sqlite import Store
from olympus.workflows import WorkflowService
from olympus.workflows.service import LocalPlanner


def stored_review(bundle, *, important=False):
    initial = MomusPlanReviewer(runner=AgentRunner(MockProvider(fixtures={
        "Momus:plan_review": {"output": {"verdict": "passed", "findings": []}},
    }))).review(bundle.plan, bundle.requirement, bundle.design).record
    findings = [ReviewFinding(
        finding_id="obsolete-program-coverage", target=bundle.plan.ref(), category="UNCOVERED_REQUIREMENT",
        severity="blocker", message="obsolete program rule reports a coverage gap", raised_by="program",
    ), ReviewFinding(
        finding_id="resolved-semantic-gap", target=bundle.plan.ref(), category="PREVIOUS_GAP",
        severity="blocker", message="preserve the previously repaired interface",
        raised_by="Momus", status="closed", closed_by="Momus",
        closure_reason=f"{bundle.plan.tasks[0].task_id} · goal：{bundle.plan.tasks[0].goal}",
    )]
    if important:
        findings.append(ReviewFinding(
            finding_id="remaining-important-gap", target=bundle.plan.ref(), category="EDGE_CASE",
            severity="important", message="include the remaining boundary case", raised_by="Momus",
        ))
    submission = initial.submission.model_copy(update={
        "verdict": "blocked", "findings": findings,
    })
    return CommonReviewer().validate_and_record(
        submission, current_target=bundle.plan.ref(), expected_author="Daedalus",
        current_inputs=[bundle.requirement.ref(), bundle.design.ref()],
    )


def make_host(tmp_path, bundle, previous):
    store = Store(tmp_path / "state.sqlite3")
    host = WorkflowService(store, RuntimeRegistry(store), lambda ledger: AgentRunner(MockProvider(), ledger=ledger))
    doc = host.create(bundle.requirement.model_dump(mode="json"), allow_synthetic=True)
    doc.update(design=bundle.design.model_dump(mode="json"), plan=bundle.plan.model_dump(mode="json"),
               plan_review=previous.review.model_dump(mode="json"),
               plan_review_record=previous.model_dump(mode="json"), stage="plan_review_blocked")
    doc["approvals"]["design"] = {"target": bundle.design.ref().model_dump(mode="json")}
    return host, doc


def test_resolved_program_only_blocker_resumes_without_regenerating_plan(tmp_path, bundle, monkeypatch):
    previous = stored_review(bundle)
    host, doc = make_host(tmp_path, bundle, previous)
    accepted = []
    monkeypatch.setattr(host, "_generate_acceptance", lambda *args: accepted.append(args[-1].ref()))

    def should_not_rewrite(*args, **kwargs):
        pytest.fail("a rule correction on the same plan must not invoke another rewrite")

    monkeypatch.setattr(LocalPlanner, "revise_plan", should_not_rewrite)
    try:
        host._generate_plan(doc, "revalidate-only")
        assert doc["stage"] == "plan_pending_approval"
        assert doc["plan"] == bundle.plan.model_dump(mode="json")
        assert doc["plan_review"]["verdict"] == "passed"
        assert accepted == [bundle.plan.ref()]
        assert "plan" not in doc["approvals"]
        assert len(doc["plan_review_history"]) == 2
        assert doc["plan_review_history"][0]["review"]["verdict"] == "blocked"
        closed = next(f for f in doc["plan_review_record"]["submission"]["findings"]
                      if f["finding_id"] == "resolved-semantic-gap")
        assert closed["status"] == "closed"
    finally:
        host.close()


def test_repair_receives_important_findings_and_resolved_nonregression_evidence(tmp_path, bundle, monkeypatch):
    previous = stored_review(bundle, important=True)
    host, doc = make_host(tmp_path, bundle, previous)
    captured = {}

    def capture(planner, plan, findings, req, ds, *, repair_context):
        captured.update(findings=findings, context=repair_context)
        raise OutputSchemaError("stop after recording the repair request")

    monkeypatch.setattr(LocalPlanner, "revise_plan", capture)
    try:
        with pytest.raises(OutputSchemaError):
            host._generate_plan(doc, "complete-repair")
        assert captured["findings"] == ["include the remaining boundary case"]
        assert [row["finding_id"] for row in captured["context"]["active_findings"]] == ["remaining-important-gap"]
        assert [row["finding_id"] for row in captured["context"]["resolved_findings"]] == ["resolved-semantic-gap"]
        assert doc["plan_revision"]["finding_count"] == 1
    finally:
        host.close()


def test_no_content_change_keeps_previous_version_and_reports_no_progress(tmp_path, bundle, monkeypatch):
    host, doc = make_host(tmp_path, bundle, stored_review(bundle, important=True))
    monkeypatch.setattr(LocalPlanner, "revise_plan", lambda self, plan, *args, **kwargs:
                        plan.model_copy(update={"version": plan.version + 1}))
    try:
        host._generate_plan(doc, "no-progress")
        assert doc["stage"] == "plan_review_blocked"
        assert doc["error"]["code"] == "PLAN_REPAIR_NO_PROGRESS"
        assert doc["plan"]["version"] == bundle.plan.version
    finally:
        host.close()

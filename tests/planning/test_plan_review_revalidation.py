"""Repair evidence is validated, and unchanged plans retain independent review results."""

import pytest

from olympus.agents.momus.planning.reviewer import MomusPlanReviewer, SemanticFinding
from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError
from olympus.contracts.models import Plan, Task, fingerprint


class ReviewProvider(MockProvider):
    mode = "real_model"

    def __init__(self, *, invalid_quote=False, important_only=False):
        findings = [{"code": "EXAMPLE", "severity": "important", "message": "Show the visible result."}]
        if not important_only:
            findings.insert(0, {"code": "RETURN_VALUE", "severity": "blocker", "message": "Specify a return value."})
        super().__init__(fixtures={"Momus:plan_review": {"output": {
            "verdict": "blocked" if not important_only else "passed", "findings": findings,
        }}})
        self.invalid_quote = invalid_quote
        self.requests = []

    def steps(self, request):
        self.requests.append(request)
        if request.task_kind != "plan_finding_check":
            return super().steps(request)
        task = request.input_manifest["tasks"][0]
        return [{"output": {"decisions": [{
            "finding_id": finding["finding_id"], "decision": "resolved", "citations": [{
                "task_id": task["task_id"], "field": "goal",
                "quote": "This quotation is absent." if self.invalid_quote else task["goal"],
            }],
        } for finding in request.input_manifest["blockers"]]}}]


def revise(plan):
    first, *rest = plan.tasks
    semantic = first.semantic_content() | {"goal": "Document the interface and its visible result"}
    task = Task(**semantic, version=first.version + 1, content_hash=fingerprint(semantic))
    tasks = [task, *rest]
    content = {"tasks": [{"task_id": task.task_id, "version": task.version, "content_hash": task.content_hash}
                         for task in tasks]}
    return Plan.model_validate({**plan.model_dump(mode="json"), "version": plan.version + 1,
                                "content": content, "content_hash": fingerprint(content), "tasks": tasks})


def test_recheck_closes_blockers_and_active_important_findings_with_current_evidence(bundle):
    provider = ReviewProvider()
    reviewer = MomusPlanReviewer(runner=AgentRunner(provider))
    original = reviewer.review(bundle.plan, bundle.requirement, bundle.design)
    result = reviewer.review(revise(bundle.plan), bundle.requirement, bundle.design, previous_review=original.record)
    rechecked = provider.requests[-1].input_manifest["blockers"]
    assert {finding["severity"] for finding in rechecked} == {"blocker", "important"}
    assert result.review.verdict == "passed"
    assert all(f.status == "closed" and f.closed_by == "Momus" for f in result.record.submission.findings)
    assert all(f.status == "open" for f in original.record.submission.findings)


def test_invalid_resolved_citation_has_actionable_format_error(bundle):
    provider = ReviewProvider(invalid_quote=True)
    reviewer = MomusPlanReviewer(runner=AgentRunner(provider))
    original = reviewer.review(bundle.plan, bundle.requirement, bundle.design)
    with pytest.raises(OutputSchemaError, match="citation") as error:
        reviewer.review(revise(bundle.plan), bundle.requirement, bundle.design, previous_review=original.record)
    assert "task_id" in error.value.hint
    assert all(f.status == "open" for f in original.record.submission.findings)


def test_program_revalidation_preserves_semantic_closures_without_model_calls(bundle, monkeypatch):
    provider = ReviewProvider()
    reviewer = MomusPlanReviewer(runner=AgentRunner(provider))
    real_program = reviewer._program_findings

    def old_program(plan, req, design):
        report, findings = real_program(plan, req, design)
        return report, [*findings, SemanticFinding(code="UNCOVERED_DESIGN", severity="blocker",
                                                   message="A testing element was rejected by the old rule.")]

    monkeypatch.setattr(reviewer, "_program_findings", old_program)
    first = reviewer.review(bundle.plan, bundle.requirement, bundle.design)
    plan = revise(bundle.plan)
    blocked = reviewer.review(plan, bundle.requirement, bundle.design, previous_review=first.record)
    assert blocked.review.verdict == "blocked"
    assert blocked.record.submission.passed_checks == ["momus:semantic-review"]
    count = len(provider.requests)
    monkeypatch.setattr(reviewer, "_program_findings", real_program)
    refreshed = reviewer.revalidate_program_checks(plan, bundle.requirement, bundle.design, blocked.record)
    assert refreshed.review.verdict == "passed"
    assert len(provider.requests) == count
    assert refreshed.review.target == blocked.review.target
    assert refreshed.review.review_id != blocked.review.review_id
    assert "REVALIDATED" in refreshed.review.review_id
    assert all(f.status == "closed" for f in refreshed.record.submission.findings)
    assert blocked.review.verdict == "blocked"


def test_program_revalidation_preserves_active_important_findings(bundle):
    provider = ReviewProvider(important_only=True)
    reviewer = MomusPlanReviewer(runner=AgentRunner(provider))
    previous = reviewer.review(bundle.plan, bundle.requirement, bundle.design)
    count = len(provider.requests)
    refreshed = reviewer.revalidate_program_checks(bundle.plan, bundle.requirement, bundle.design, previous.record)
    assert len(provider.requests) == count
    assert refreshed.record.submission.findings[0].severity == "important"
    assert refreshed.record.submission.findings[0].status == "open"


def test_program_revalidation_rejects_changed_plan_or_missing_independent_evidence(bundle):
    reviewer = MomusPlanReviewer(runner=AgentRunner(ReviewProvider(important_only=True)))
    previous = reviewer.review(bundle.plan, bundle.requirement, bundle.design)
    with pytest.raises(ValueError, match="exact"):
        reviewer.revalidate_program_checks(revise(bundle.plan), bundle.requirement, bundle.design, previous.record)
    damaged = previous.record.model_copy(deep=True)
    damaged.submission.evidence = [item for item in damaged.submission.evidence if not item.startswith("ER-")]
    damaged.review.evidence = damaged.submission.evidence
    with pytest.raises(ValueError, match="evidence"):
        reviewer.revalidate_program_checks(bundle.plan, bundle.requirement, bundle.design, damaged)

"""Review delivery obligations and actual report contracts without inventing artifacts."""

from olympus.agents.momus.planning import MomusPlanReviewer
from olympus.agents.runtime import AgentRunner, MockProvider
from olympus.execution.profile import LOCAL_EXECUTION_CAPABILITIES


class CapturingReviewerProvider(MockProvider):
    def __init__(self, finding=None):
        super().__init__(fixtures={"Momus:plan_review": {"output": {
            "verdict": "blocked" if finding else "passed", "findings": [finding] if finding else [],
        }}})
        self.requests = []

    def steps(self, request):
        self.requests.append(request)
        return super().steps(request)


def test_review_manifest_uses_approved_delivery_scope_real_report_counts_and_full_input_list(bundle):
    provider = CapturingReviewerProvider()
    reviewer = MomusPlanReviewer(runner=AgentRunner(provider), review_context=LOCAL_EXECUTION_CAPABILITIES)
    reviewer.review(bundle.plan, bundle.requirement, bundle.design)
    manifest = provider.requests[0].input_manifest
    prompt = manifest["prompt"]
    assert manifest["execution_context"] == LOCAL_EXECUTION_CAPABILITIES
    assert manifest["tasks"] == [task.model_dump(mode="json") for task in bundle.plan.tasks]
    assert "approved requirement and design define scope" in prompt
    assert "complete README.md" in prompt and "Chinese run-instruction" in prompt
    assert "additional standalone guide is required only if" in prompt
    assert "['data']['checks'][i]['output']" in prompt and "exit_code" in prompt
    assert "actual observed counts" in prompt and "requirement-backed expected values" in prompt
    assert "ENTIRE current task.inputs list" in prompt
    assert "do not require a passed verdict" in prompt


def test_execution_context_does_not_suppress_a_real_independent_blocker(bundle):
    finding = {"code": "REQUIRED_FAILURE_CASE", "severity": "blocker", "task_id": "T01",
               "message": "The required failure case is missing from the current task checks."}
    provider = CapturingReviewerProvider(finding)
    reviewer = MomusPlanReviewer(runner=AgentRunner(provider), review_context=LOCAL_EXECUTION_CAPABILITIES)
    result = reviewer.review(bundle.plan, bundle.requirement, bundle.design)
    assert result.review.verdict == "blocked"
    assert any(item.code == "REQUIRED_FAILURE_CASE" for item in result.findings)
    assert result.record.blocking_finding_ids

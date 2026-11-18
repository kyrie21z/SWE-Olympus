"""Human retries repair actual failures using verified same-task evidence."""

from copy import deepcopy
from dataclasses import replace

import pytest

from olympus.agents.runtime import MockProvider
from olympus.commands.runtime_handlers.handlers import RuntimeCommandHandlers
from olympus.contracts.models import Command, Task, fingerprint
from olympus.execution.adapter import bytes_hash

from test_local_executor import SIMPLE_CHECK, SIMPLE_SOURCE, dispatch_and_wait, make_world

BAD_SOURCE = 'def states():\n    return ("WRONG",)\n'


class RetryProvider(MockProvider):
    def __init__(self, *, argus_blocked=False):
        super().__init__(fixtures={
            "Hephaestus:implement": {"output": {"changes": [
                {"path": "packages/t01/contract.py", "content": SIMPLE_SOURCE if argus_blocked else BAD_SOURCE}]}},
            "Argus:artifact_review": {"output": {"verdict": "blocked" if argus_blocked else "passed", "findings": [
                {"code": "DOC_REQUIRED", "severity": "blocker", "message": "Document the Interface contract explicitly."}
            ] if argus_blocked else []}},
        })
        self.prompts = []

    def invoke(self, request, step, **kwargs):
        if request.role_id == "Hephaestus":
            self.prompts.append(deepcopy(request.input_manifest))
            if "previous_attempt" in request.input_manifest["input_content"]:
                return super().invoke(request, {"output": {"changes": [{
                    "path": "packages/t01/contract.py", "content": SIMPLE_SOURCE,
                }]}}, **kwargs)
        return super().invoke(request, step, **kwargs)


def human_retry(scheduler, task_id="T01"):
    before = scheduler.ctx.run.calls_used
    result = RuntimeCommandHandlers().retry(Command(
        operation_id=f"human-retry-{task_id}", run_id=scheduler.ctx.run.run_id,
        actor="YOU", action="retry", target_id=task_id,
        expected_version=scheduler.tasks[task_id].version,
        expected_state_revision=scheduler.ctx.run.state_revision,
        parameters={"prompt": "Fix the actual failed check without changing acceptance."},
    ), scheduler.ctx, scheduler)
    assert result[0] == "accepted" and scheduler.ctx.run.calls_used == before


def test_actual_failure_human_retry_supplies_verified_evidence_and_all_frozen_checks_pass(bundle, tmp_path):
    provider = RetryProvider()
    check = "# FROZEN_CHECK_SOURCE_MUST_NOT_BE_SENT\n" + SIMPLE_CHECK
    scheduler, adapter, service, _ = make_world(bundle, tmp_path, provider=provider, check=check)
    original_acceptance = adapter._definitions["T01"][0].model_dump(mode="json")
    first = dispatch_and_wait(scheduler, adapter)
    assert first.state == "failed"
    assert service.collect("T01", first.attempt_id)[0] == "rejected"
    assert len(provider.prompts) == 1 and "previous_attempt" not in provider.prompts[0]["input_content"]
    human_retry(scheduler)
    second = dispatch_and_wait(scheduler, adapter)
    assert second.state == "succeeded", second.error
    repair = provider.prompts[1]["input_content"]["previous_attempt"]
    assert repair["attempt_id"] == first.attempt_id and repair["generation"] == first.generation
    assert repair["error"].startswith("ValueError: actual acceptance check failed")
    assert repair["failed_checks"] == [{
        "check_id": first.check_outputs[0]["check_id"], "exit_code": first.check_outputs[0]["exit_code"],
        "output_tail": (adapter.root / first.attempt_id / "check_0.log").read_bytes()[-4096:].decode("utf-8", "replace"),
        "output_verified": True,
    }]
    actual_source = (adapter.root / first.attempt_id / "work/packages/t01/contract.py").read_bytes()
    assert repair["files"] == {"packages/t01/contract.py": {
        "sha256": bytes_hash(actual_source), "content": actual_source.decode("utf-8")}}
    assert "FROZEN_CHECK_SOURCE_MUST_NOT_BE_SENT" not in str(repair)
    assert adapter._definitions["T01"][0].model_dump(mode="json") == original_acceptance
    assert second.checks_passed == scheduler.tasks["T01"].checks
    assert service.collect("T01", second.attempt_id)[0] == "accepted"
    assert (adapter.root / first.attempt_id / "work/packages/t01/contract.py").read_text(encoding="utf-8") == BAD_SOURCE
    assert (adapter.root / second.attempt_id / "work/packages/t01/contract.py").read_text(encoding="utf-8") == SIMPLE_SOURCE


def test_argus_rejection_findings_are_bound_to_previous_artifact_and_forwarded(bundle, tmp_path):
    provider = RetryProvider(argus_blocked=True)
    scheduler, adapter, service, _ = make_world(bundle, tmp_path, provider=provider)
    first = dispatch_and_wait(scheduler, adapter)
    assert first.state == "succeeded" and service.collect("T01", first.attempt_id)[0] == "rejected"
    assert adapter.query_receipt(first.attempt_id).review is not None
    provider.fixtures["Argus:artifact_review"]["output"] = {"verdict": "passed", "findings": []}
    human_retry(scheduler)
    second = dispatch_and_wait(scheduler, adapter)
    context = provider.prompts[1]["input_content"]["previous_attempt"]
    assert context["failed_checks"] == []
    assert context["review"]["verdict"] == "blocked"
    assert context["review"]["findings"][0]["category"] == "DOC_REQUIRED"
    assert context["review"]["findings"][0]["message"] == "Document the Interface contract explicitly."
    assert service.collect("T01", second.attempt_id)[0] == "accepted"


@pytest.mark.parametrize("difference", ["generation_one", "task", "task_hash", "run", "unknown", "in_flight"])
def test_context_never_borrows_unrelated_changed_or_unresolved_attempt(bundle, tmp_path, difference):
    scheduler, adapter, _, _ = make_world(bundle, tmp_path, source=BAD_SOURCE)
    first = dispatch_and_wait(scheduler, adapter)
    previous = adapter._jobs[first.attempt_id]
    receipt = first.model_copy(deep=True, update={"generation": first.generation + 1})
    current = replace(previous, receipt=receipt)
    if difference == "generation_one":
        current.receipt.generation = 1
    elif difference in {"task", "task_hash"}:
        content = previous.task.semantic_content() | ({"task_id": "T02"} if difference == "task" else {"goal": "Changed task"})
        current.task = Task(**content, version=2, content_hash=fingerprint(content))
        current.receipt.task_id, current.receipt.task_hash = current.task.task_id, current.task.content_hash
    elif difference == "run":
        current.receipt.run_id = "OTHER-RUN"
    elif difference == "unknown":
        previous.receipt.state = "outcome_unknown"
    elif difference == "in_flight":
        previous.done.clear()
    assert adapter._previous_attempt_context(current) is None


def test_tampered_files_and_check_logs_are_not_forwarded_as_verified_repair_context(bundle, tmp_path):
    _, adapter, _, _ = make_world(bundle, tmp_path, source=BAD_SOURCE)
    scheduler = adapter.scheduler
    first = dispatch_and_wait(scheduler, adapter)
    previous = adapter._jobs[first.attempt_id]
    (adapter.root / first.attempt_id / "work/packages/t01/contract.py").write_text("secret tampered source", encoding="utf-8")
    (adapter.root / first.attempt_id / "check_0.log").write_text("secret tampered log", encoding="utf-8")
    current = replace(previous, receipt=first.model_copy(deep=True, update={"generation": first.generation + 1}))
    context = adapter._previous_attempt_context(current)
    assert context["files"] == {} and context["omitted_files"] == ["packages/t01/contract.py"]
    assert context["failed_checks"][0]["output_tail"] == ""
    assert context["failed_checks"][0]["output_verified"] is False
    assert "secret tampered" not in str(context)

from pathlib import Path
import threading
import time

import pytest

from olympus.agents.argus import ArgusReviewer
from olympus.agents.hephaestus import HephaestusImplementer
from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.agents.runtime import AgentRunner, MockProvider
from olympus.contracts.models import Ref
from olympus.execution import ArtifactAcceptanceService, LocalPythonExecutor
from olympus.execution.policy import validate_python
from olympus.orchestrator.scheduler import Scheduler
from olympus.orchestrator.state_machine import RunContext
from olympus.planning.service import PlanService

ROOT = Path(__file__).resolve().parents[2]
SIMPLE_SOURCE = 'def states():\n    return ("CONFIRMED", "WAITLISTED", "CANCELLED")\n'
SIMPLE_CHECK = ('from packages.t01.contract import states\n'
                'assert states() == ("CONFIRMED", "WAITLISTED", "CANCELLED")\n')


def make_world(bundle, tmp_path, *, source=SIMPLE_SOURCE, check=SIMPLE_CHECK,
               path="packages/t01/contract.py", verdict="passed", findings=None,
               task_id="T01", timeout_cap=20, provider=None, run_id=None):
    run = bundle.run.model_copy(deep=True)
    run.mode = "real_tool"
    run.status = "active"
    if run_id is not None:
        run.run_id = run_id
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    ctx = RunContext(run=run)
    scheduler = Scheduler(bundle.plan, package.manifest, ctx)
    scheduler.reconcile()
    fixture_outputs = {
        "Hephaestus:implement": {"output": {"changes": [{"path": path, "content": source}]}},
        "Argus:artifact_review": {"output": {"verdict": verdict, "findings": findings or []}},
    }
    if task_id == "T03":
        fixture_outputs["Hephaestus:T01"] = {"output": {"changes": [
            {"path": "packages/t01/contract.py", "content": SIMPLE_SOURCE}]}}
    provider = provider or MockProvider(fixtures=fixture_outputs)
    runner = AgentRunner(provider)
    adapter = LocalPythonExecutor(scheduler, tmp_path / "executor", HephaestusImplementer(runner),
                                  timeout_cap=timeout_cap)
    frozen = FrozenAcceptance.freeze(scheduler.tasks[task_id],
                                     {c: check for c in scheduler.tasks[task_id].checks})
    adapter.register_task(task_id, frozen, context={bundle.design.object_id: bundle.design.content})
    acceptance = ArtifactAcceptanceService(adapter, ArgusReviewer(runner))
    if task_id == "T03":
        interface_task = scheduler.tasks["T01"]
        adapter.register_task("T01", FrozenAcceptance.freeze(interface_task,
                              {c: SIMPLE_CHECK for c in interface_task.checks}))
        scheduler.dispatch("T01", adapter)
        first = adapter.wait(scheduler.attempt_for("T01"))
        assert first.state == "succeeded", first.error
        assert acceptance.collect("T01", first.attempt_id)[0] == "accepted"
    return scheduler, adapter, acceptance, runner


def dispatch_and_wait(scheduler, adapter, task_id="T01"):
    assert scheduler.dispatch(task_id, adapter) is adapter
    attempt = scheduler.attempt_for(task_id)
    return adapter.wait(attempt)


def test_real_python_execution_collects_files_outputs_and_only_then_accepts(bundle, tmp_path):
    scheduler, adapter, service, runner = make_world(bundle, tmp_path)
    receipt = dispatch_and_wait(scheduler, adapter)
    assert receipt.state == "succeeded", receipt.error
    assert receipt.tool_mode == "real_tool" and receipt.model_mode == "mock"
    assert receipt.artifacts and receipt.check_outputs[0]["exit_code"] == 0
    assert scheduler.ctx.state_of("T01") == "running"
    assert service.collect("T01", receipt.attempt_id)[0] == "accepted"
    assert scheduler.ctx.state_of("T01") == "accepted"
    assert scheduler.ctx.state_of("T02") == "ready"
    assert runner.records.list_by_role("Argus")
    assert adapter.project(receipt.attempt_id)["file_hashes"]


@pytest.mark.parametrize("path", ["../outside.py", "tests/acceptance.py", "checks/check_0.py", "C:/secret.py"])
def test_out_of_scope_implementation_is_rejected_before_writing(bundle, tmp_path, path):
    scheduler, adapter, service, _ = make_world(bundle, tmp_path, path=path)
    receipt = dispatch_and_wait(scheduler, adapter)
    assert receipt.state == "failed"
    assert service.collect("T01", receipt.attempt_id)[0] == "rejected"
    assert scheduler.ctx.state_of("T01") == "failed"
    assert not (tmp_path / "outside.py").exists()
    assert scheduler.ctx.state_of("T02") == "waiting"


def test_program_failure_is_not_overridden_by_argus_passed(bundle, tmp_path):
    scheduler, adapter, service, runner = make_world(bundle, tmp_path, check="assert False, 'independent failure'")
    receipt = dispatch_and_wait(scheduler, adapter)
    assert receipt.state == "failed"
    assert receipt.check_outputs[0]["exit_code"] != 0
    assert "independent failure" in receipt.check_outputs[0]["output"]
    assert service.collect("T01", receipt.attempt_id)[0] == "rejected"
    assert not runner.records.list_by_role("Argus")


def test_argus_blocker_prevents_acceptance(bundle, tmp_path):
    findings = [{"code": "BUSINESS_MISMATCH", "severity": "blocker", "message": "contract not satisfied"}]
    scheduler, adapter, service, _ = make_world(bundle, tmp_path, verdict="blocked", findings=findings)
    receipt = dispatch_and_wait(scheduler, adapter)
    assert receipt.state == "succeeded"
    assert service.collect("T01", receipt.attempt_id)[0] == "rejected"
    assert scheduler.ctx.state_of("T01") == "failed"
    assert scheduler.ctx.state_of("T02") == "waiting"


def test_changed_artifact_invalidates_old_checks(bundle, tmp_path):
    scheduler, adapter, service, _ = make_world(bundle, tmp_path)
    receipt = dispatch_and_wait(scheduler, adapter)
    file = adapter.root / receipt.attempt_id / "work/packages/t01/contract.py"
    file.write_text("def states(): return ()", encoding="utf-8")
    assert service.collect("T01", receipt.attempt_id)[0] == "rejected"
    assert scheduler.ctx.state_of("T01") != "accepted"


def test_frozen_acceptance_or_log_tampering_is_detected(bundle, tmp_path):
    scheduler, adapter, service, _ = make_world(bundle, tmp_path)
    receipt = dispatch_and_wait(scheduler, adapter)
    path = adapter.root / receipt.attempt_id / "checks/check_0.py"
    path.write_text("pass", encoding="utf-8")
    assert service.collect("T01", receipt.attempt_id)[0] == "rejected"


def test_stale_generation_and_cancelled_task_do_not_accept_late_result(bundle, tmp_path):
    scheduler, adapter, service, _ = make_world(bundle, tmp_path)
    receipt = dispatch_and_wait(scheduler, adapter)
    scheduler.cancel("T01")
    assert service.collect("T01", receipt.attempt_id)[0] == "rejected"
    assert scheduler.ctx.state_of("T01") == "cancelled"


def test_old_independent_report_cannot_accept_current_artifact(bundle, tmp_path):
    scheduler, adapter, service, _ = make_world(bundle, tmp_path)
    receipt = dispatch_and_wait(scheduler, adapter)
    actual = adapter.verify_integrity(receipt.attempt_id)
    report = service.reviewer.review(scheduler.tasks["T01"], receipt, actual_files=actual)
    report.review.target = Ref(object_id=report.review.target.object_id, version=99, content_hash="a" * 64)
    assert service.collect("T01", receipt.attempt_id, review_id=report.review.review_id)[0] == "rejected"
    assert scheduler.ctx.state_of("T01") == "verifying"


def test_query_and_idempotent_submit_do_not_execute_twice(bundle, tmp_path):
    scheduler, adapter, _, runner = make_world(bundle, tmp_path)
    receipt = dispatch_and_wait(scheduler, adapter)
    again = adapter.submit("T01", receipt.generation, operation_id="another-click")
    assert again.attempt_id == receipt.attempt_id
    assert adapter.query(receipt.attempt_id) == "succeeded"
    assert len(runner.records.list_by_role("Hephaestus")) == 2  # model plus actual tool record


def test_execution_requires_permit_registered_acceptance_and_explicit_mode(bundle, tmp_path):
    scheduler, adapter, _, _ = make_world(bundle, tmp_path)
    with pytest.raises(ValueError, match="permit"):
        adapter.submit("T01", 1)
    scheduler.ctx.run.mode = "mock"
    with pytest.raises(ValueError, match="real"):
        scheduler.dispatch("T01", adapter)
    assert scheduler.ctx.state_of("T01") == "ready"
    assert scheduler.permit_for("T01") is None


def test_timeout_terminates_local_process_and_retains_actual_log(bundle, tmp_path):
    scheduler, adapter, service, _ = make_world(bundle, tmp_path,
        source="while True:\n    pass\n", timeout_cap=0.3)
    receipt = dispatch_and_wait(scheduler, adapter)
    assert receipt.state == "failed" and "timeout" in receipt.error
    assert service.collect("T01", receipt.attempt_id)[0] == "rejected"


def test_cancel_live_local_process_confirms_termination(bundle, tmp_path):
    scheduler, adapter, service, _ = make_world(bundle, tmp_path,
        source="while True:\n    pass\n")
    scheduler.dispatch("T01", adapter)
    attempt = scheduler.attempt_for("T01")
    deadline = time.monotonic() + 10
    while adapter._jobs[attempt].process is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert adapter._jobs[attempt].process is not None
    receipt = adapter.cancel(attempt)
    assert receipt.state == "cancelled" and receipt.cancel_confirmed
    assert adapter.wait(attempt).state == "cancelled"
    assert service.collect("T01", attempt)[0] == "rejected"


def test_unknown_model_outcome_retains_resource_and_is_queryable(bundle, tmp_path):
    provider = MockProvider(fixtures={"Hephaestus:implement": {"status": "outcome_unknown"}})
    scheduler, adapter, service, _ = make_world(bundle, tmp_path, provider=provider)
    receipt = dispatch_and_wait(scheduler, adapter)
    assert receipt.state == "outcome_unknown"
    assert service.collect("T01", receipt.attempt_id)[0] == "pending"
    assert scheduler.permit_for("T01") is not None
    assert adapter.ledger.query(receipt.request_id).budget_held
    cancelled = adapter.cancel(receipt.attempt_id)
    assert cancelled.state == "outcome_unknown" and not cancelled.cancel_confirmed


def test_executor_restart_preserves_receipts_without_resending(bundle, tmp_path):
    scheduler, adapter, _, runner = make_world(bundle, tmp_path)
    receipt = dispatch_and_wait(scheduler, adapter)
    recovered = LocalPythonExecutor(scheduler, adapter.root, HephaestusImplementer(runner))
    assert recovered.query(receipt.attempt_id) == "succeeded"
    assert recovered.verify_integrity(receipt.attempt_id)
    assert len(recovered.ledger.entries()) == 1


@pytest.mark.parametrize("source", ["import os", "open('x','w')", "eval('1')",
                                    "import subprocess", "x=(1).__class__", "import threading"])
def test_registered_python_profile_rejects_privileged_operations(source):
    with pytest.raises(ValueError):
        validate_python(source)


def test_frozen_definition_hash_is_checked(bundle):
    frozen = FrozenAcceptance.freeze(bundle.plan.tasks[0], {c: "pass" for c in bundle.plan.tasks[0].checks})
    frozen.checks[next(iter(frozen.checks))] = "assert False"
    with pytest.raises(ValueError, match="changed"):
        frozen.verify(bundle.plan.tasks[0])


def test_real_campus_capacity_cancel_and_promotion_checks(bundle, tmp_path):
    scheduler, adapter, service, _ = make_world(bundle, tmp_path, task_id="T03",
        source=(ROOT / "fixtures/execution/campus_service.py").read_text(encoding="utf-8"),
        check=(ROOT / "fixtures/execution/check_campus.py").read_text(encoding="utf-8"),
        path="packages/t03/campus.py")
    receipt = dispatch_and_wait(scheduler, adapter, "T03")
    assert receipt.state == "succeeded", receipt.error
    assert "Ran 4 tests" in receipt.check_outputs[0]["output"]
    assert service.collect("T03", receipt.attempt_id)[0] == "accepted"
    assert scheduler.ctx.state_of("T05") == "ready"


def test_budget_exhaustion_keeps_artifact_unverified_without_another_model_call(bundle, tmp_path):
    scheduler, adapter, service, runner = make_world(bundle, tmp_path)
    scheduler.ctx.run.call_budget = 1
    receipt = dispatch_and_wait(scheduler, adapter)
    assert receipt.state == "succeeded"
    assert scheduler.ctx.run.calls_used == 1
    assert service.collect("T01", receipt.attempt_id)[0] == "pending"
    assert scheduler.ctx.state_of("T01") == "verifying"
    assert len(runner.ledger.entries()) == 1


def test_changed_check_log_is_rejected(bundle, tmp_path):
    scheduler, adapter, service, _ = make_world(bundle, tmp_path)
    receipt = dispatch_and_wait(scheduler, adapter)
    (adapter.root / receipt.attempt_id / "check_0.log").write_text("pretend passed", encoding="utf-8")
    assert service.collect("T01", receipt.attempt_id)[0] == "rejected"


def test_refreshed_artifact_needs_fresh_checks_and_independent_report(bundle, tmp_path):
    scheduler, adapter, service, runner = make_world(bundle, tmp_path,
        verdict="blocked", findings=[{"code": "FIX", "severity": "blocker", "message": "repair required"}])
    old = dispatch_and_wait(scheduler, adapter)
    assert service.collect("T01", old.attempt_id)[0] == "rejected"
    old_review = service.reviewer.common.list_reviews()[-1].review.review_id
    scheduler.ctx.transition("T01", "ready", reason="authorized repair fixture", actor="YOU")
    runner.provider.fixtures["Hephaestus:implement"]["output"]["changes"][0]["content"] = (
        'def states():\n    result = ("CONFIRMED", "WAITLISTED", "CANCELLED")\n    return result\n')
    runner.provider.fixtures["Argus:artifact_review"]["output"] = {"verdict": "passed", "findings": []}
    new = dispatch_and_wait(scheduler, adapter)
    assert new.generation == old.generation + 1
    assert new.artifacts[0].content_hash != old.artifacts[0].content_hash
    assert service.collect("T01", new.attempt_id, review_id=old_review)[0] == "rejected"
    assert scheduler.ctx.state_of("T01") == "verifying"
    assert service.collect("T01", new.attempt_id)[0] == "accepted"


def test_cancellation_can_win_while_argus_is_reviewing(bundle, tmp_path):
    entered, release = threading.Event(), threading.Event()
    class BlockingArgus(MockProvider):
        def invoke(self, request, step, **kwargs):
            if request.role_id == "Argus":
                entered.set()
                assert release.wait(10)
            return super().invoke(request, step, **kwargs)
    provider = BlockingArgus(fixtures={
        "Hephaestus:implement": {"output": {"changes": [
            {"path": "packages/t01/contract.py", "content": SIMPLE_SOURCE}]}},
        "Argus:artifact_review": {"output": {"verdict": "passed", "findings": []}},
    })
    scheduler, adapter, service, _ = make_world(bundle, tmp_path, provider=provider)
    receipt = dispatch_and_wait(scheduler, adapter)
    result = []
    thread = threading.Thread(target=lambda: result.append(service.collect("T01", receipt.attempt_id)))
    thread.start()
    try:
        assert entered.wait(10)
        scheduler.cancel("T01")
    finally:
        release.set()
        thread.join(10)
    assert result[0][0] == "rejected"
    assert scheduler.ctx.state_of("T01") == "cancelled"


def test_arbitrary_missing_upstream_receipt_is_not_replaced_by_a_success_claim(bundle, tmp_path):
    scheduler, adapter, _, _ = make_world(bundle, tmp_path)
    scheduler.ctx.transition("T01", "running", reason="synthetic setup", actor="YOU")
    scheduler.ctx.transition("T01", "verifying", reason="synthetic setup", actor="YOU")
    scheduler.ctx.transition("T01", "accepted", reason="synthetic setup", actor="YOU", verification="passed")
    scheduler.reconcile()
    task = scheduler.tasks["T02"]
    adapter.register_task("T02", FrozenAcceptance.freeze(task, {c: "pass" for c in task.checks}))
    with pytest.raises(ValueError, match="artifact bytes"):
        scheduler.dispatch("T02", adapter)
    assert scheduler.ctx.state_of("T02") == "ready"


def test_momus_test_task_runs_frozen_checks_without_impersonating_implementation(bundle, tmp_path):
    source = (ROOT / "fixtures/execution/campus_service.py").read_text(encoding="utf-8")
    script = (ROOT / "fixtures/execution/check_campus.py").read_text(encoding="utf-8")
    scheduler, adapter, service, runner = make_world(bundle, tmp_path, task_id="T03",
        source=source, check=script, path="packages/t03/campus.py")
    third = dispatch_and_wait(scheduler, adapter, "T03")
    assert service.collect("T03", third.attempt_id)[0] == "accepted"
    runner.provider.fixtures["Hephaestus:T02"] = {"output": {"changes": [
        {"path": "packages/t02/campus.py", "content": source}]}}
    second = scheduler.tasks["T02"]
    adapter.register_task("T02", FrozenAcceptance.freeze(second,
        {c: script.replace("packages.t03.campus", "packages.t02.campus") for c in second.checks}))
    done = dispatch_and_wait(scheduler, adapter, "T02")
    assert service.collect("T02", done.attempt_id)[0] == "accepted"
    task = scheduler.tasks["T06"]
    adapter.register_task("T06", FrozenAcceptance.freeze(task, {c: script for c in task.checks}))
    receipt = dispatch_and_wait(scheduler, adapter, "T06")
    assert receipt.state == "succeeded", receipt.error
    assert receipt.model_mode == "not_called"
    assert not [r for r in runner.records.list_by_role("Hephaestus") if r.task_id == "T06"]
    assert any("check-report.json" in f for f in receipt.file_hashes)
    assert all(a.task_id == "T06" for a in receipt.artifacts)
    assert service.collect("T06", receipt.attempt_id)[0] == "accepted"
    record = service.reviewer.common.list_reviews()[-1]
    assert record.review.author == "Momus"
    assert runner.records.list_by_role("Momus")[-1].mode == "real_tool"


def test_distinct_runs_share_evidence_root_without_reusing_receipts_or_model_keys(bundle, tmp_path):
    scheduler, adapter, _, runner = make_world(bundle, tmp_path)
    old = dispatch_and_wait(scheduler, adapter)
    second_scheduler, _, _, _ = make_world(bundle, tmp_path / "other", run_id="RUN-second")
    second = LocalPythonExecutor(second_scheduler, adapter.root, HephaestusImplementer(runner))
    task = second_scheduler.tasks["T01"]
    second.register_task("T01", FrozenAcceptance.freeze(task, {c: SIMPLE_CHECK for c in task.checks}),
                         context={bundle.design.object_id: bundle.design.content})
    with pytest.raises(KeyError):
        second.query(old.attempt_id)
    current = dispatch_and_wait(second_scheduler, second)
    assert current.state == "succeeded", current.error
    assert current.run_id == "RUN-second" and current.request_id != old.request_id
    assert current.attempt_id != old.attempt_id
    assert len(runner.records.list_by_role("Hephaestus")) == 4


def test_submission_alias_is_persisted_and_cannot_be_reused_for_another_task(bundle, tmp_path):
    scheduler, adapter, service, runner = make_world(bundle, tmp_path)
    receipt = dispatch_and_wait(scheduler, adapter)
    assert service.collect("T01", receipt.attempt_id)[0] == "accepted"
    adapter.submit("T01", receipt.generation, operation_id="second-click")
    restored = LocalPythonExecutor(scheduler, adapter.root, HephaestusImplementer(runner))
    for task_id in ["T01", "T02"]:
        task = scheduler.tasks[task_id]
        restored.register_task(task_id, FrozenAcceptance.freeze(task, {c: SIMPLE_CHECK for c in task.checks}),
                               context={bundle.design.object_id: bundle.design.content})
    assert restored.submit("T01", receipt.generation, operation_id="second-click").attempt_id == receipt.attempt_id
    with pytest.raises(ValueError, match="idempotency"):
        restored.submit("T02", 1, operation_id="second-click")
    assert len(restored.ledger.entries()) == 1


def test_acceptance_report_is_queryable_after_restart_and_duplicate_collect_is_idempotent(bundle, tmp_path):
    scheduler, adapter, service, runner = make_world(bundle, tmp_path)
    receipt = dispatch_and_wait(scheduler, adapter)
    original = service.collect("T01", receipt.attempt_id)
    calls, events = len(runner.ledger.entries()), len(scheduler.ctx.events)
    restored = LocalPythonExecutor(scheduler, adapter.root, HephaestusImplementer(runner))
    view = ArtifactAcceptanceService(restored, ArgusReviewer(runner))
    assert view.collect("T01", receipt.attempt_id) == original
    data = view.project("T01")
    assert data["is_current"] and data["runtime"]["state"] == "accepted"
    assert data["execution"]["review"]["review"]["verdict"] == "passed"
    assert data["execution"]["tool_invocations"] >= 1
    assert len(runner.ledger.entries()) == calls and len(scheduler.ctx.events) == events


def test_concurrent_acceptance_does_not_send_duplicate_argus_requests(bundle, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    scheduler, adapter, service, runner = make_world(bundle, tmp_path)
    receipt = dispatch_and_wait(scheduler, adapter)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.collect("T01", receipt.attempt_id), range(2)))
    assert results[0] == results[1] and results[0][0] == "accepted"
    assert len(runner.records.list_by_role("Argus")) == 1
    assert sum(e.task_id == "T01" and e.new_state == "accepted" for e in scheduler.ctx.events) == 1


def test_unknown_argus_is_exposed_for_p5_and_not_blindly_resent(bundle, tmp_path):
    scheduler, adapter, service, runner = make_world(bundle, tmp_path)
    receipt = dispatch_and_wait(scheduler, adapter)
    runner.provider.fixtures["Argus:artifact_review"] = {"status": "outcome_unknown"}
    assert service.collect("T01", receipt.attempt_id)[0] == "pending"
    calls = len(runner.ledger.entries())
    assert service.collect("T01", receipt.attempt_id)[0] == "pending"
    assert len(runner.ledger.entries()) == calls
    view = service.project("T01")
    assert view["runtime"]["state"] == "verifying"
    assert view["review_requests"][0]["needs_reconciliation"]


def test_failure_before_process_start_does_not_claim_a_real_tool_invocation(bundle, tmp_path):
    scheduler, adapter, _, runner = make_world(bundle, tmp_path, source="import os")
    receipt = dispatch_and_wait(scheduler, adapter)
    assert receipt.state == "failed" and receipt.tool_invocations == 0
    assert runner.records.get_by_attempt(receipt.attempt_id).call_count == 0
    assert not list((adapter.root / receipt.attempt_id).glob("check_*.log"))


def test_receipt_persistence_failure_finishes_as_failure_instead_of_hanging(bundle, tmp_path, monkeypatch):
    scheduler, adapter, service, _ = make_world(bundle, tmp_path)
    save = adapter._save
    def fail_success_save(job):
        if job.receipt.state == "succeeded":
            raise OSError("simulated evidence disk failure")
        save(job)
    monkeypatch.setattr(adapter, "_save", fail_success_save)
    receipt = dispatch_and_wait(scheduler, adapter)
    assert receipt.state == "failed" and "evidence_persist_failed" in receipt.error
    assert service.collect("T01", receipt.attempt_id)[0] == "rejected"
    assert scheduler.ctx.state_of("T01") == "failed"

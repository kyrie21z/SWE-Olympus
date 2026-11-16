"""P5 失败路径场景：迟到/重复、风险接受、修复回接。

issue #9 要求这些不是靠单元断言，而是"无界面场景脚本"，
所以这里从构建运行态开始，一路走到可观察的结果。
"""

from __future__ import annotations

from pathlib import Path

from olympus.commands.runtime_handlers import RuntimeCommandHandlers
from olympus.contracts.models import Command
from olympus.orchestrator.recovery import RuntimeRegistry, plan_repair
from olympus.orchestrator.state_machine import RunContext
from olympus.planning.service import PlanService
from olympus.requests.ledger import RequestLedger
from olympus.storage.sqlite import Store

ACCEPT_PATH = ("ready", "running", "verifying", "accepted")


def build(tmp_path: Path, bundle):
    store = Store(tmp_path / "state.sqlite3")
    store.create(bundle.run)
    runtime = RuntimeRegistry(store).get(bundle.run.run_id)
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    runtime.attach_plan(bundle.plan, package.manifest)
    runtime.bus.dispatch(
        Command(
            operation_id="op-start",
            run_id=bundle.run.run_id,
            actor="YOU",
            action="start",
            target_id=bundle.run.run_id,
            expected_version=1,
            expected_state_revision=0,
            parameters={},
        )
    )
    return runtime


def accept(ctx: RunContext, task_id: str) -> None:
    for state in ACCEPT_PATH:
        if ctx.state_of(task_id) == state:
            continue
        ctx.transition(
            task_id,
            state,
            reason="场景推进",
            actor="Argus" if state == "accepted" else "Zeus",
            actor_type="agent" if state == "accepted" else "program",
            verification="passed" if state == "accepted" else None,
        )


def test_late_response_after_cancel_is_ignored():
    ledger = RequestLedger()
    entry = ledger.prepare(
        attempt_id="ATT-1",
        logical_task_id="T03",
        run_id="RUN-1",
        task_id="T03",
        role_id="Hephaestus",
        task_kind="implement",
        input_hash="a" * 64,
        idempotency_key="key-1",
    )
    ledger.mark_sent(entry.request_id, provider_request_id="prov-1")
    ledger.cancel(entry.request_id, provider_cancel_hook=lambda _: "cancelled")

    late = ledger.record_outcome(entry.request_id, "succeeded")

    assert late.status == "cancelled"
    assert late.late_response_ignored is True
    assert late.late_response_reason == "late_or_stale_generation"


def test_risk_acceptance_does_not_unlock_successors(tmp_path, bundle):
    runtime = build(tmp_path, bundle)
    accept(runtime.ctx, "T01")
    runtime.scheduler.reconcile()
    accept(runtime.ctx, "T02")
    accept(runtime.ctx, "T03")
    runtime.scheduler.reconcile()
    assert runtime.ctx.state_of("T05") == "ready"

    runtime.ctx.transition("T05", "blocked", reason="验证缺失", actor="Zeus")
    handlers: RuntimeCommandHandlers = runtime.handlers
    receipt = runtime.bus.dispatch(
        Command(
            operation_id="op-risk",
            run_id=bundle.run.run_id,
            actor="YOU",
            action="accept_risk",
            target_id="T05",
            expected_version=runtime.scheduler.tasks["T05"].version,
            expected_state_revision=runtime.ctx.run.state_revision,
            parameters={"reason": "时间不足，人工接受未验证"},
        )
    )

    assert receipt.status == "accepted"
    runtime.scheduler.reconcile()
    # T07 依赖 T05；风险接受不是验收，所以 T07 仍不可开工。
    assert runtime.ctx.state_of("T07") == "waiting"
    assert handlers.trail(bundle.run.run_id)[-1]["reason"] == "时间不足，人工接受未验证"


def test_repair_unlocks_dependents_only_after_acceptance(tmp_path, bundle):
    runtime = build(tmp_path, bundle)
    accept(runtime.ctx, "T01")
    runtime.scheduler.reconcile()
    accept(runtime.ctx, "T02")
    accept(runtime.ctx, "T03")
    runtime.scheduler.reconcile()

    plan_repair(runtime, "T03", failure_ref="REC-001", reason="重复递补")
    assert runtime.ctx.state_of("T03-R1") == "ready"
    # 修复任务还没验收，后继必须继续等。
    assert runtime.ctx.state_of("T05") == "waiting"
    assert runtime.ctx.state_of("T06") == "waiting"

    accept(runtime.ctx, "T03-R1")
    runtime.scheduler.reconcile()

    assert runtime.ctx.state_of("T05") == "ready"
    assert runtime.ctx.state_of("T06") == "ready"
    # T07 还等 T05 验收，不能提前解锁。
    assert runtime.ctx.state_of("T07") == "waiting"

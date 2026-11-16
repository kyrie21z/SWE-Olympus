"""四个运行期处理器的行为测试。

每条断言都对应 issue #9 的一句要求：

- 重试：回到 ready、代际 +1、**总预算不重置**、提示原文保留；
- 风险接受：**不增加 accepted、不解锁后继**，理由必填；
- 改派：状态不变、产物仍走同一验收；
- 变更请求：只登记并标注回退层级，不擅自改计划。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from olympus.contracts.models import Command
from olympus.orchestrator.recovery import RunRuntime, RuntimeRegistry
from olympus.planning.service import PlanService
from olympus.storage.sqlite import Store


def build_runtime(tmp_path: Path, bundle) -> RunRuntime:
    store = Store(tmp_path / "state.sqlite3")
    store.create(bundle.run)
    runtime = RuntimeRegistry(store).get(bundle.run.run_id)
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    runtime.attach_plan(bundle.plan, package.manifest)
    return runtime


def start(runtime: RunRuntime, bundle, operation_id: str = "op-start") -> None:
    receipt = runtime.bus.dispatch(
        Command(
            operation_id=operation_id,
            run_id=bundle.run.run_id,
            actor="YOU",
            action="start",
            target_id=bundle.run.run_id,
            expected_version=1,
            expected_state_revision=0,
            parameters={},
        )
    )
    assert receipt.status == "accepted"


def task_command(runtime: RunRuntime, bundle, action: str, *, target: str = "T01", **parameters) -> Command:
    task = next(task for task in bundle.plan.tasks if task.task_id == target)
    return Command(
        operation_id=f"op-{action}-{uuid4().hex[:8]}",
        run_id=bundle.run.run_id,
        actor="YOU",
        action=action,
        target_id=target,
        expected_version=task.version,
        expected_state_revision=runtime.ctx.run.state_revision,
        parameters=parameters,
    )


def block(runtime: RunRuntime, task_id: str = "T01") -> None:
    runtime.ctx.transition(task_id, "blocked", reason="测试阻塞", actor="Zeus")


def test_handlers_replace_deferred_actions(tmp_path, bundle):
    runtime = build_runtime(tmp_path, bundle)

    catalog = {row["action"]: row for row in runtime.bus.catalog()}

    for action in ("retry", "reassign", "accept_risk", "request_change"):
        assert catalog[action]["implemented"] is True


def test_retry_returns_to_ready_and_keeps_budget(tmp_path, bundle):
    runtime = build_runtime(tmp_path, bundle)
    start(runtime, bundle)
    block(runtime)
    budget_before = runtime.ctx.run.calls_used
    generation_before = runtime.ctx.runtime("T01").generation

    receipt = runtime.bus.dispatch(
        task_command(runtime, bundle, "retry", prompt="递补前对候补记录加行锁")
    )

    assert receipt.status == "accepted"
    task = runtime.ctx.runtime("T01")
    assert task.state == "ready"
    assert task.generation == generation_before + 1
    assert task.verification_status == "unverified"
    # 人工重试另记，但不重置总预算。
    assert runtime.ctx.run.calls_used == budget_before
    trail = runtime.handlers.trail(bundle.run.run_id)
    assert trail[-1]["prompt"] == "递补前对候补记录加行锁"
    assert trail[-1]["human_retry_count"] == 1


def test_retry_requires_failed_or_blocked(tmp_path, bundle):
    runtime = build_runtime(tmp_path, bundle)
    start(runtime, bundle)  # T01 此时是 ready

    receipt = runtime.bus.dispatch(task_command(runtime, bundle, "retry"))

    assert receipt.status == "rejected"
    assert receipt.reason_code == "bad_state"


def test_retry_requires_running_run(tmp_path, bundle):
    runtime = build_runtime(tmp_path, bundle)  # 仍是 draft
    block(runtime)

    receipt = runtime.bus.dispatch(task_command(runtime, bundle, "retry"))

    assert receipt.status == "rejected"
    assert receipt.reason_code == "bad_state"


def test_accept_risk_requires_reason(tmp_path, bundle):
    runtime = build_runtime(tmp_path, bundle)
    start(runtime, bundle)
    block(runtime)

    receipt = runtime.bus.dispatch(task_command(runtime, bundle, "accept_risk"))

    assert receipt.status == "rejected"
    assert receipt.reason_code == "bad_state"


def test_accept_risk_does_not_verify_or_unlock(tmp_path, bundle):
    runtime = build_runtime(tmp_path, bundle)
    start(runtime, bundle)
    block(runtime)
    accepted_before = sum(1 for t in runtime.ctx.run.tasks if t.state == "accepted")

    receipt = runtime.bus.dispatch(
        task_command(runtime, bundle, "accept_risk", reason="时间不足，人工接受未验证")
    )

    assert receipt.status == "accepted"
    task = runtime.ctx.runtime("T01")
    assert task.state == "cancelled"          # 终态，但不是 accepted
    assert task.disposition == "risk_accepted"
    assert task.verification_status == "unverified"
    assert sum(1 for t in runtime.ctx.run.tasks if t.state == "accepted") == accepted_before

    promoted = runtime.scheduler.reconcile()
    assert "T02" not in promoted               # 后继靠 accepted 解锁，风险接受不顶替


def test_reassign_records_assignment_without_state_change(tmp_path, bundle):
    runtime = build_runtime(tmp_path, bundle)
    start(runtime, bundle)
    revision_before = runtime.ctx.run.state_revision

    receipt = runtime.bus.dispatch(task_command(runtime, bundle, "reassign", assignee="YOU"))

    assert receipt.status == "accepted"
    assert runtime.ctx.runtime("T01").state == "ready"
    assert runtime.ctx.run.state_revision == revision_before + 1
    assert runtime.handlers.assignments(bundle.run.run_id) == {"T01": "YOU"}


def test_reassign_requires_assignee(tmp_path, bundle):
    runtime = build_runtime(tmp_path, bundle)
    start(runtime, bundle)

    receipt = runtime.bus.dispatch(task_command(runtime, bundle, "reassign"))

    assert receipt.status == "rejected"
    assert receipt.reason_code == "bad_state"


def test_request_change_rejects_unknown_level(tmp_path, bundle):
    runtime = build_runtime(tmp_path, bundle)
    start(runtime, bundle)

    receipt = runtime.bus.dispatch(
        task_command(runtime, bundle, "request_change", level="everything", summary="随便改")
    )

    assert receipt.status == "rejected"
    assert receipt.reason_code == "bad_state"


def test_request_change_records_upstream_route(tmp_path, bundle):
    runtime = build_runtime(tmp_path, bundle)
    start(runtime, bundle)
    revision_before = runtime.ctx.run.state_revision

    receipt = runtime.bus.dispatch(
        Command(
            operation_id=f"op-change-{uuid4().hex[:8]}",
            run_id=bundle.run.run_id,
            actor="YOU",
            action="request_change",
            target_id=bundle.run.run_id,
            expected_version=bundle.plan.version,
            expected_state_revision=runtime.ctx.run.state_revision,
            parameters={"level": "requirement", "summary": "执行中追加短信通知"},
        )
    )

    assert receipt.status == "accepted"
    assert receipt.reason_code == "recorded_upstream"
    # 只登记，不改任务状态；当前运行继续。
    assert runtime.ctx.run.state_revision == revision_before + 1
    assert runtime.ctx.run.status == "active"
    entry = runtime.handlers.trail(bundle.run.run_id)[-1]
    assert entry["level"] == "requirement"
    assert entry["summary"] == "执行中追加短信通知"

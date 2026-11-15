"""P3 编排层测试：OL-05 动态调度 + OL-06 命令总线。

全部用团队既有产物：`fixtures/contracts/valid.json` 的 Plan / Run，
`PlanService.prepare` 产出的 `CompiledManifest`。不另造任务、不另造契约。

覆盖：OW-06 前驱验收后并行 / OW-07 资源与执行者冲突 / OW-08 未验收不解锁 /
OW-11 未知结果保留资源 / OW-12 暂停停派发 / OW-13 重复命令同回执 /
OW-17 版本过期拒绝 / OW-19 越权拒绝 / 事件与快照一致 / 接受风险不冒充已验证。
"""

from __future__ import annotations

import pytest
from olympus.commands.core.bus import CommandBus
from olympus.contracts.models import Command
from olympus.orchestrator.scheduler import Scheduler
from olympus.orchestrator.state_machine import RunContext
from olympus.planning.service import PlanService


@pytest.fixture
def world(bundle):
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    ctx = RunContext(run=bundle.run)
    sched = Scheduler(plan=bundle.plan, manifest=package.manifest, ctx=ctx)
    bus = CommandBus(ctx=ctx, scheduler=sched)
    return ctx, sched, bus, package


def cmd(ctx, action, target="RUN-campus", actor="YOU", rev=None, version=None,
        op=None, parameters=None, run_id=None):
    return Command(
        operation_id=op or f"op-{action}-{ctx.run.state_revision}",
        run_id=run_id or ctx.run.run_id,
        actor=actor,
        action=action,                      # type: ignore[arg-type]
        target_id=target,
        expected_version=version if version is not None else 1,
        expected_state_revision=rev if rev is not None else ctx.run.state_revision,
        parameters=parameters or {},
    )


def accept(sched, task_id, reviewer="Argus"):
    """模拟一次正常的提交与独立复核（不属于命令动作，走 Artifact/Review 服务）。

    提交通过 `Scheduler.submit` 完成：它会先释放执行槽再转 verifying——
    这是"六个人跑 8 个任务"的前提。
    """
    sched.submit(task_id)
    sched.ctx.transition(task_id, "accepted", reason="独立复核通过", actor=reviewer,
                         actor_type="agent", verification="passed")


# ---------------------------------------------------------------- 调度
def test_initial_reconcile_only_unblocks_layer_zero(world):
    ctx, sched, _bus, _pkg = world
    promoted = sched.reconcile()
    assert promoted == ["T01"], "只有无依赖的 T01 变 ready"
    assert ctx.state_of("T02") == "waiting"
    assert next(t["task_id"] for t in sched.projection()["tasks"]) == "T01"


def test_dispatch_requires_active_run(world):
    ctx, sched, bus, _pkg = world
    sched.reconcile()
    reasons = dict(sched.dispatchable())["T01"]
    assert any("draft" in r for r in reasons)
    assert sched.acquire("T01") is None
    receipt = bus.dispatch(cmd(ctx, "start"))
    assert receipt.status == "accepted"
    assert ctx.run.status == "active"
    assert sched.acquire("T01") is not None


def test_priority_rank_orders_ready_set(world):
    ctx, sched, bus, _pkg = world
    bus.dispatch(cmd(ctx, "start"))
    sched.reconcile()
    sched.acquire("T01")
    accept(sched, "T01")
    sched.reconcile()
    # manifest 的 priority_rank：T03=2 < T02=3 < T04=4
    assert sched.ready() == ["T03", "T02", "T04"]


def test_executor_capacity_serializes_same_executor(world):
    ctx, sched, bus, _pkg = world
    bus.dispatch(cmd(ctx, "start"))
    sched.reconcile()
    sched.acquire("T01")
    accept(sched, "T01")
    sched.reconcile()
    assert sched.acquire("T03") is not None          # Hephaestus 被占
    blocked = dict(sched.dispatchable())
    assert any("Hephaestus" in r for r in blocked["T02"]), "同一执行者只放一个"


def test_parallel_when_executor_capacity_allows(bundle):
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    ctx = RunContext(run=bundle.run)
    sched = Scheduler(plan=bundle.plan, manifest=package.manifest, ctx=ctx,
                      max_parallel=3, executor_capacity=3)
    bus = CommandBus(ctx=ctx, scheduler=sched)
    bus.dispatch(cmd(ctx, "start"))
    sched.reconcile()
    sched.acquire("T01")
    accept(sched, "T01")
    sched.reconcile()
    got = [sched.acquire(tid) for tid in ("T03", "T02", "T04")]
    assert all(p is not None for p in got), "执行者容量允许时可并行"


def test_write_scope_overlap_blocks_dispatch(world):
    ctx, sched, bus, _pkg = world
    bus.dispatch(cmd(ctx, "start"))
    sched.reconcile()
    sched.acquire("T01")
    accept(sched, "T01")
    sched.reconcile()
    # 造一个与 T03 写范围重叠的视图：只改调度器使用的任务表，不改计划
    overlapping = sched.tasks["T02"].model_copy(update={"write_scope": ["packages/t03/sub"]})
    sched.plan = sched.plan.model_copy(update={"tasks": [
        overlapping if t.task_id == "T02" else t for t in sched.plan.tasks
    ]})
    sched.acquire("T03")
    reasons = dict(sched.dispatchable())["T02"]
    assert any("写范围冲突" in r for r in reasons)


def test_unknown_outcome_keeps_executor_slot(world):
    ctx, sched, bus, _pkg = world
    bus.dispatch(cmd(ctx, "start"))
    sched.reconcile()
    sched.acquire("T01")
    ctx.transition("T01", "blocked", reason="适配器超时，结果未知", actor="Zeus")
    sched.release("T01", keep_resources=True)
    assert sched._executor_load.get("Hephaestus") == 1, "结果未知时执行者槽位保留"
    sched.release("T01")
    assert sched._executor_load.get("Hephaestus", 0) == 0


def test_duplicate_dispatch_takes_one_permit(world):
    """重复派发只占一份许可（issue #5 验收 1）。"""
    ctx, sched, bus, _pkg = world
    bus.dispatch(cmd(ctx, "start"))
    sched.reconcile()

    first = sched.acquire("T01")
    second = sched.acquire("T01")
    assert first is not None and first.generation == 1
    assert second is None, "第二次派发拿不到许可"
    assert len(sched._permits) == 1, "只占一份"
    assert sched._executor_load.get("Hephaestus") == 1, "执行者只被记一次"

# ---------------------------------------------------------------- 命令
def test_command_permissions_and_state(world):
    ctx, _sched, bus, _pkg = world
    assert bus.dispatch(cmd(ctx, "start", actor="Hephaestus")).reason_code == "forbidden"
    assert bus.dispatch(cmd(ctx, "pause")).reason_code == "bad_state", "draft 不能暂停"
    assert bus.dispatch(cmd(ctx, "confirm_delivery")).reason_code == "bad_state"


def test_command_idempotency(world):
    ctx, _sched, bus, _pkg = world
    first = bus.dispatch(cmd(ctx, "start", op="op-fixed"))
    second = bus.dispatch(cmd(ctx, "start", op="op-fixed", rev=first.state_revision))
    assert first.status == second.status == "accepted"
    assert first.state_revision == second.state_revision, "同 operation_id 不重复执行"
    again = bus.dispatch(Command(operation_id="op-fixed", run_id=ctx.run.run_id, actor="YOU",
                                 action="pause", target_id="RUN-campus", expected_version=1,
                                 expected_state_revision=ctx.run.state_revision, parameters={}))
    assert again.reason_code == "conflict", "同 ID 不同载荷判冲突"


def test_stale_revision_rejected(world):
    ctx, _sched, bus, _pkg = world
    bus.dispatch(cmd(ctx, "start"))
    stale = bus.dispatch(cmd(ctx, "pause", rev=0))
    assert stale.status == "rejected" and stale.reason_code == "stale"
    assert ctx.run.status == "active", "过期命令不改状态"


def test_pause_stops_dispatch_but_keeps_inflight(world):
    ctx, sched, bus, _pkg = world
    bus.dispatch(cmd(ctx, "start"))
    sched.reconcile()
    sched.acquire("T01")
    receipt = bus.dispatch(cmd(ctx, "pause"))
    assert receipt.status == "accepted" and ctx.run.status == "paused"
    assert sched.acquire("T02") is None
    assert sched._executor_load.get("Hephaestus") == 1, "在途许可不被撤销"


def test_deferred_actions_return_not_ready(world):
    """按 issue #6：retry/reassign/accept_risk/request_change 移交 P5，本段只返回 NOT_READY。

    重点不是"没做"，而是"明确说没做"——不能假装实现。
    """
    ctx, sched, bus, _pkg = world
    bus.dispatch(cmd(ctx, "start"))
    sched.reconcile()
    sched.acquire("T01")
    ctx.transition("T01", "failed", reason="检查不通过", actor="Zeus")

    for action in ("retry", "reassign", "accept_risk", "request_change"):
        receipt = bus.dispatch(cmd(ctx, action, target="T01", op=f"op-{action}"))
        assert receipt.status == "rejected", action
        assert receipt.reason_code == "not_ready", action
        assert "P5" in bus.notes[f"op-{action}"], action

    # 未实现的动作在清单里就是 implemented=False
    rows = {r["action"]: r for r in bus.catalog()}
    assert rows["retry"]["implemented"] is False
    assert rows["start"]["implemented"] is True


def test_p5_registers_handler_without_touching_core(world):
    """P5 通过 register() 接管即可，核心命令核一行不改（issue #6 的验收点）。"""
    from olympus.contracts.models import Run

    ctx, sched, bus, _pkg = world
    bus.dispatch(cmd(ctx, "start"))
    sched.reconcile()

    def p5_retry(command, context, scheduler):
        task_id = command.target_id
        state = context.state_of(task_id)
        if state not in ("failed", "blocked"):
            raise ValueError(f"{task_id} 当前 {state}，只有 failed/blocked 可重试")
        note = str(command.parameters.get("note", "")).strip()
        if not note:
            raise ValueError("人工重试须在 parameters.note 说明原因")

        def mutate(snapshot: Run) -> None:
            runtime = next(t for t in snapshot.tasks if t.task_id == task_id)
            runtime.generation += 1
            runtime.state = "ready"
            runtime.verification_status = "unverified"

        context.commit(reason=f"人工重试：{note}", actor=command.actor,
                       actor_type="human", mutate=mutate, task_id=task_id)
        return "accepted", "ok", f"{task_id} 回到 ready"

    # 注册前不可用
    sched.acquire("T01")
    ctx.transition("T01", "failed", reason="检查不通过", actor="Zeus")
    before = bus.dispatch(cmd(ctx, "retry", target="T01", op="op-pre"))
    assert before.reason_code == "not_ready"

    # 注册后立即可用，且不修改 CommandBus
    bus.register("retry", p5_retry, {"YOU"}, "任务 failed/blocked", "回到 ready，new attempt")
    sched.reconcile()
    after = bus.dispatch(cmd(ctx, "retry", target="T01", op="op-post",
                             parameters={"note": "已补检查证据"}))
    assert after.status == "accepted", bus.notes.get("op-post")
    assert ctx.state_of("T01") == "ready"
    assert bus.catalog()[[r["action"] for r in bus.catalog()].index("retry")]["implemented"] is True

def test_confirm_delivery_requires_all_required_accepted(world):
    """交付前置：还有必需任务未验收时不能交付。"""
    ctx, sched, bus, _pkg = world
    bus.dispatch(cmd(ctx, "start"))
    sched.reconcile()
    sched.acquire("T01")
    accept(sched, "T01")
    rejected = bus.dispatch(cmd(ctx, "confirm_delivery"))
    assert rejected.reason_code == "bad_state", "还有未验收任务时不能交付"

    # 走完整合法路径把其余任务推完：ready → running → verifying → accepted
    sched.executor_capacity = 8
    sched.max_parallel = 8
    for _ in range(20):
        sched.reconcile()
        progress = False
        for task_id in list(sched.ready()):
            if sched.acquire(task_id) is None:
                continue
            progress = True
            sched.submit(task_id)
            ctx.transition(task_id, "accepted", reason="复核通过", actor="Argus",
                           actor_type="agent", verification="passed")
        if not progress:
            break
    assert all(ctx.state_of(t.task_id) == "accepted" for t in sched.plan.tasks)
    done = bus.dispatch(cmd(ctx, "confirm_delivery"))
    assert done.status == "accepted"
    assert ctx.run.status == "completed"

# ---------------------------------------------------------------- 事件与投影
def test_every_event_carries_consistent_snapshot(world):
    ctx, sched, bus, _pkg = world
    bus.dispatch(cmd(ctx, "start"))
    sched.reconcile()
    sched.acquire("T01")
    assert ctx.events, "至少产生一条事件"
    for event in ctx.events:
        assert event.run_id == event.snapshot.run_id
        assert event.event_seq == event.snapshot.event_seq
        assert event.state_revision == event.snapshot.state_revision
        assert event.event_seq <= ctx.run.event_seq
    assert [e.event_seq for e in ctx.events] == sorted(e.event_seq for e in ctx.events), "事件序号单调"


def test_projection_reports_progress_and_blockers(world):
    ctx, sched, bus, _pkg = world
    bus.dispatch(cmd(ctx, "start"))
    sched.reconcile()
    sched.acquire("T01")
    accept(sched, "T01")
    sched.reconcile()
    view = sched.projection()
    assert view["progress"] == {"accepted": 1, "required": 8}
    assert view["run"]["state_revision"] == ctx.run.state_revision
    assert view["manifest"]["manifest_id"]
    blocked = {item["task_id"] for item in view["sets"]["blocked"]}
    assert "T02" in blocked
    assert view["resources"]["inflight"] == 0

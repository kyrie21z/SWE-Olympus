"""调度场景测试：执行替身、代际、取消与验收竞争（issue #5 的场景验收）。

与 `tests/orchestrator/scheduler/test_scheduler.py` 的分工：
- 那边测"单个能力"（三集合、排序、原子许可）
- 这边测"一条真实时间线"：派出去 → 查询 → 结果不明 → 迟到回执 → 取消与验收撞车

issue #5 的验收原句：
> 取消/改版与验收竞争只允许合法迁移，旧代际/迟到回执不能覆盖新状态。
> 未知在途保留额度/资源直到核实或明确隔离；本模块先用 fake executor 验证。
> T01验收后T02/03/04可就绪；T03 verifying 时 T05 waiting，退出码零不解锁。
"""

from __future__ import annotations

import pytest
from olympus.commands.core.bus import CommandBus
from olympus.contracts.models import Command
from olympus.orchestrator.scheduler import FakeExecutor, Scheduler
from olympus.orchestrator.state_machine import IllegalTransition, RunContext
from olympus.planning.service import PlanService


def accept(sched, task_id, *, generation=1, artifacts=None):
    """带程序检查证据的接纳——契约要求 checks 必须覆盖。"""
    task = sched.tasks[task_id]
    return sched.accept_submission(
        task_id, generation, artifacts or [f"artifact:{task_id}"],
        checks_passed=list(task.checks))


@pytest.fixture
def world(bundle):
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    ctx = RunContext(run=bundle.run)
    sched = Scheduler(plan=bundle.plan, manifest=package.manifest, ctx=ctx)
    bus = CommandBus(ctx=ctx, scheduler=sched)
    bus.dispatch(Command(operation_id="op-start", run_id=ctx.run.run_id, actor="YOU",
                         action="start", target_id=ctx.run.run_id, expected_version=1,
                         expected_state_revision=0, parameters={}))
    sched.reconcile()
    return ctx, sched, bus


# ---------------------------------------------------------------- 执行替身
def test_fake_executor_full_cycle(world):
    """派发 → 查询 running → 执行成功 → 带当前代际提交 → accepted。"""
    ctx, sched, _bus = world
    executor = FakeExecutor()
    sched.dispatch("T01", executor)
    attempt_id = sched._attempts["T01"]

    assert executor.query(attempt_id) == "running", "刚派发时在途"
    assert ctx.state_of("T01") == "running"

    executor.finish(attempt_id, "succeeded", ["A01"])
    status, reason = accept(sched, "T01", artifacts=["A01"])
    assert status == "accepted", reason
    assert ctx.state_of("T01") == "verifying"
    assert sched._executor_load.get("Hephaestus", 0) == 0, "提交即释放执行槽"

    ctx.transition("T01", "accepted", reason="复核通过", actor="Argus",
                   actor_type="agent", verification="passed")
    sched.reconcile()
    assert sched.ready() == ["T03", "T02", "T04"], "T01 验收后三个后继就绪"


def test_unknown_outcome_keeps_resources(world):
    """结果未知：资源不退还，直到核实。"""
    ctx, sched, _bus = world
    executor = FakeExecutor()
    sched.dispatch("T01", executor)
    attempt_id = sched._attempts["T01"]

    executor.cancel(attempt_id, provider_confirms=False)     # 远端未确认
    assert executor.query(attempt_id) == "outcome_unknown"

    sched.settle_unknown("T01", verified_running=True)
    assert ctx.state_of("T01") == "blocked"
    assert "T01" in sched._permits, "结果未知时不释放许可"
    assert sched._executor_load.get("Hephaestus") == 1

    sched.release("T01")                                      # 核实结束
    assert sched._executor_load.get("Hephaestus", 0) == 0


# ---------------------------------------------------------------- 代际与迟到回执
def test_late_receipt_from_old_generation_is_stale(world):
    """旧代际的产物不能覆盖新状态。"""
    ctx, sched, _bus = world
    sched.acquire("T01")                       # generation 1
    ctx.transition("T01", "failed", reason="检查未通过", actor="Zeus")
    sched.release("T01")
    assert ctx.runtime("T01").generation == 1

    # failed → ready（合法边），再次 acquire 时调度器自动把代际 +1
    ctx.transition("T01", "ready", reason="人工重试", actor="YOU", actor_type="human")
    sched.acquire("T01")                       # generation 2
    assert ctx.runtime("T01").generation == 2

    status, reason = accept(sched, "T01", artifacts=["A01"])
    assert status == "stale", reason
    assert ctx.state_of("T01") == "running", "迟到的旧产物不改状态"


# ---------------------------------------------------------------- 取消与验收竞争
def test_cancel_after_accept_is_rejected(world):
    """验收之后不能取消。"""
    ctx, sched, _bus = world
    sched.acquire("T01")
    sched.submit("T01")
    ctx.transition("T01", "accepted", reason="复核通过", actor="Argus",
                   actor_type="agent", verification="passed")

    status, reason = sched.cancel("T01")
    assert status == "rejected" and "终态" in reason
    assert ctx.state_of("T01") == "accepted"


def test_accept_after_cancel_is_rejected(world):
    """取消之后不接受产物。"""
    ctx, sched, _bus = world
    sched.acquire("T01")
    status, _ = sched.cancel("T01")
    assert status == "accepted" and ctx.state_of("T01") == "cancelled"

    status, reason = accept(sched, "T01", artifacts=["A01"])
    assert status == "rejected" and "不在执行中" in reason


def test_illegal_transition_is_blocked_by_state_machine(world):
    """绕过调度器直接改状态，会被状态机拦住。"""
    ctx, _sched, _bus = world
    with pytest.raises(IllegalTransition):
        ctx.transition("T01", "accepted", reason="跳过产物", actor="Zeus",
                       verification="passed")


# ---------------------------------------------------------------- 解锁语义
def test_verifying_does_not_unlock_successors(world):
    """T03 verifying 时 T05 仍 waiting；退出码零不等于 accepted。"""
    ctx, sched, _bus = world
    sched.acquire("T01")
    sched.submit("T01")
    ctx.transition("T01", "accepted", reason="复核通过", actor="Argus",
                   actor_type="agent", verification="passed")
    sched.reconcile()

    executor = FakeExecutor()
    sched.dispatch("T03", executor)
    executor.finish(sched._attempts["T03"], "succeeded", ["A03"])
    accept(sched, "T03", artifacts=["A03"])

    assert ctx.state_of("T03") == "verifying", "产物已交但未复核"
    assert ctx.state_of("T05") == "waiting", "T05 不能因 T03 提交而解锁"
    assert "T05" not in sched.ready()


def test_failed_predecessor_blocks_only_dependents(world):
    """T02 失败只阻塞依赖它的任务，独立支线继续。"""
    ctx, sched, _bus = world
    sched.acquire("T01")
    sched.submit("T01")
    ctx.transition("T01", "accepted", reason="复核通过", actor="Argus",
                   actor_type="agent", verification="passed")
    sched.reconcile()

    sched.acquire("T02")
    ctx.transition("T02", "failed", reason="界面测试不通过", actor="Zeus")
    sched.release("T02")
    sched.reconcile()

    assert ctx.state_of("T06") == "waiting", "T06 依赖 T02，仍等待"
    assert "T03" in sched.ready(), "T03 与 T02 无依赖，不受影响"
    assert sched.acquire("T03") is not None

# ---------------------------------------------------------------- 局部异常与"退出码零"
def test_incident_does_not_stop_independent_branch(world):
    """一条支线登记异常后，另一条独立支线照常派发。

    issue #5 原文："局部异常登记后继续独立支线"
    """
    ctx, sched, _bus = world
    sched.acquire("T01")
    sched.submit("T01")
    ctx.transition("T01", "accepted", reason="复核通过", actor="Argus",
                   actor_type="agent", verification="passed")
    sched.reconcile()

    # T02 执行失败：登记异常 + 置 failed，只影响依赖它的 T06
    sched.acquire("T02")
    sched.register_incident("T02", "execution_failed", "报名服务检查不通过")
    ctx.transition("T02", "failed", reason="检查不通过", actor="Zeus")
    sched.release("T02")

    # 独立支线 T03 仍可派发
    sched.reconcile()
    assert "T03" in sched.ready()
    assert sched.acquire("T03") is not None, "T03 与 T02 无依赖，异常不阻塞它"

    # 异常进入待人工队列，且投影里可见
    open_items = sched.open_incidents()
    assert [i.task_id for i in open_items] == ["T02"]
    assert open_items[0].kind == "execution_failed"
    view = sched.projection()
    assert view["incidents"]["open"][0]["task_id"] == "T02"

    # 处置后从待人工队列消失
    sched.resolve_incident("T02", "已补检查证据")
    assert sched.open_incidents() == []
    assert sched.projection()["incidents"]["resolved"] == {"T02": "已补检查证据"}


def test_zero_exit_code_does_not_unlock(world):
    """执行器返回 succeeded（等价退出码零）不等于 accepted，不解锁后继。

    issue #5 原文："T03 verifying 时 T05 waiting，退出码零不解锁"
    """
    ctx, sched, _bus = world
    sched.acquire("T01")
    sched.submit("T01")
    ctx.transition("T01", "accepted", reason="复核通过", actor="Argus",
                   actor_type="agent", verification="passed")
    sched.reconcile()

    executor = FakeExecutor()
    sched.dispatch("T03", executor)
    attempt_id = sched._attempts["T03"]
    executor.finish(attempt_id, "succeeded", ["A03"])
    assert executor.query(attempt_id) == "succeeded", "执行器说成功了"

    status, _ = accept(sched, "T03", artifacts=["A03"])
    assert status == "accepted"
    assert ctx.state_of("T03") == "verifying", "成功 ≠ 已验收"
    assert ctx.runtime("T03").verification_status == "unverified"

    # 下游 T05 仍等，且不在就绪集
    assert ctx.state_of("T05") == "waiting"
    assert "T05" not in sched.ready()
    assert dict(sched.dispatchable()).get("T05") in (None, []) or "T05" not in dict(sched.dispatchable())

    # 只有独立复核通过才解锁
    ctx.transition("T03", "accepted", reason="复核通过", actor="P3",
                   actor_type="human", verification="passed")
    sched.reconcile()
    assert ctx.state_of("T05") == "ready", "复核通过后 T05 才就绪"

# ---------------------------------------------------------------- 改版失效
def test_revision_invalidates_accepted_and_successors(world):
    """改版让已验收任务回退、验收标 stale，后继一并失效。

    issue #5 原文："取消/**改版**与验收竞争只允许合法迁移"
    """
    from olympus.orchestrator.state_machine import invalidate_revision

    ctx, sched, _bus = world
    sched.acquire("T01")
    sched.submit("T01")
    ctx.transition("T01", "accepted", reason="复核通过", actor="Argus",
                   actor_type="agent", verification="passed")
    sched.reconcile()
    assert ctx.runtime("T01").verification_status == "passed"

    # 改版：T01 的接口契约变了，影响 T01 及其全部后继
    outcome = invalidate_revision(
        ctx, sched.plan, sched.manifest, roots=["T01"], reason="接口字段改名")

    assert outcome.roots == ["T01"]
    assert "T01" in outcome.invalidated
    runtime = ctx.runtime("T01")
    assert runtime.state == "waiting", "已验收任务回到 waiting（不是直接删掉）"
    assert runtime.verification_status == "stale", "验收标为过期，而不是 passed"
    assert runtime.generation == 2, "改版后重新执行算新代际"

    # 后继同样回退
    for task_id in ("T02", "T03", "T04"):
        assert ctx.state_of(task_id) == "waiting", f"{task_id} 因前驱失效回到 waiting"

    # 事件流记下了这次改版
    assert any("改版失效" in e.reason for e in ctx.events)


def test_revision_keeps_unaffected_branch(world):
    """只改一条支线时，另一条已验收的任务不受影响。"""
    from olympus.orchestrator.state_machine import invalidate_revision

    ctx, sched, _bus = world
    sched.executor_capacity = 4
    sched.max_parallel = 4
    for task_id in ("T01", "T02", "T03"):
        sched.reconcile()
        assert sched.acquire(task_id) is not None, task_id
        sched.submit(task_id)
        ctx.transition(task_id, "accepted", reason="复核通过", actor="Argus",
                       actor_type="agent", verification="passed")

    # T05 只依赖 T03；改 T05 不影响 T02
    outcome = invalidate_revision(
        ctx, sched.plan, sched.manifest, roots=["T05"], reason="页面契约调整")
    assert ctx.state_of("T02") == "accepted", "与 T05 无关的支线保留验收"
    assert "T02" in outcome.kept or "T02" not in outcome.invalidated

# ---------------------------------------------------------------- 原子接纳：任务/输入/报告
def test_accept_rejects_missing_inputs(world):
    """输入产物未验收时，任务产出的东西不能接纳。

    issue #5 原文："原子接纳检查任务/**输入**/报告"
    """
    ctx, sched, _bus = world
    sched.acquire("T01")
    sched.submit("T01")     # T01 产物冻结，但**未验收** → artifact:T01 还没"产出"
    sched.reconcile()

    # 构造"只声明输入、没声明前驱"的任务：这正是 depends_on 与 inputs 的差别。
    # 依赖图里 T02 仍等 T01，但把它的前驱抹掉后就可派——此时输入仍缺。
    predecessors = dict(sched.manifest.predecessors)
    predecessors["T02"] = []
    sched.manifest = sched.manifest.model_copy(update={"predecessors": predecessors})
    ctx.transition("T02", "ready", reason="测试用：只声明输入，不声明前驱", actor="YOU",
                   actor_type="human")
    assert sched.acquire("T02") is not None
    assert sched.missing_inputs("T02") == ["artifact:T01"]

    status, reason = accept(sched, "T02")
    assert status == "rejected" and "输入未就绪" in reason
    assert ctx.state_of("T02") == "running", "拒绝接纳不改状态"

    # T01 验收后，输入就绪
    ctx.transition("T01", "accepted", reason="复核通过", actor="Argus",
                   actor_type="agent", verification="passed")
    assert sched.missing_inputs("T02") == []
    status, _ = accept(sched, "T02")
    assert status == "accepted"


def test_accept_rejects_missing_check_evidence(world):
    """有产物但没有程序检查证据，不能接纳。

    issue #5 原文："原子接纳检查任务/输入/**报告**"
    """
    ctx, sched, _bus = world
    sched.acquire("T01")
    task = sched.tasks["T01"]
    status, reason = sched.accept_submission("T01", 1, ["artifact:T01"])
    assert status == "rejected" and "缺少程序检查证据" in reason
    assert "check:T01" in reason
    assert ctx.state_of("T01") == "running"

    status, _ = sched.accept_submission("T01", 1, ["artifact:T01"],
                                        checks_passed=list(task.checks))
    assert status == "accepted"


def test_stale_task_version_rejected(world):
    """命令带的任务版本与当前不符时判 stale_version（接口/版本不匹配用例）。"""
    ctx, _sched, bus = world
    bus.dispatch(Command(operation_id="op-start", run_id=ctx.run.run_id, actor="YOU",
                         action="start", target_id=ctx.run.run_id, expected_version=1,
                         expected_state_revision=ctx.run.state_revision, parameters={}))
    stale = bus.dispatch(Command(operation_id="op-pause-bad", run_id=ctx.run.run_id,
                                 actor="YOU", action="pause", target_id="RUN-campus",
                                 expected_version=99,
                                 expected_state_revision=ctx.run.state_revision,
                                 parameters={}))
    assert stale.status == "rejected" and stale.reason_code == "stale_version"
    assert "计划版本" in bus.notes["op-pause-bad"]

def test_unknown_inflight_prevents_fully_stopped(world):
    """暂停 + 未核实在途：不能显示"全部已停"。

    issue #6 原文："暂停后不新增许可，在途仍可接纳收尾；**未知未核实不显示全部已停**。"
    """
    ctx, sched, bus = world
    bus.dispatch(Command(operation_id="op-start", run_id=ctx.run.run_id, actor="YOU",
                         action="start", target_id=ctx.run.run_id, expected_version=1,
                         expected_state_revision=ctx.run.state_revision, parameters={}))
    sched.reconcile()

    executor = FakeExecutor()
    sched.dispatch("T01", executor)
    attempt = sched._attempts["T01"]
    executor.cancel(attempt, provider_confirms=False)      # 远端未确认
    sched.register_incident("T01", "outcome_unknown", "适配器超时，结果未核实")
    sched.settle_unknown("T01", verified_running=True)

    bus.dispatch(Command(operation_id="op-pause", run_id=ctx.run.run_id, actor="YOU",
                         action="pause", target_id=ctx.run.run_id, expected_version=1,
                         expected_state_revision=ctx.run.state_revision, parameters={}))

    view = sched.projection()
    assert view["pause_state"]["paused"] is True
    assert view["pause_state"]["unsettled"] == ["T01"]
    assert view["pause_state"]["fully_stopped"] is False, "还有未核实的在途，不能报全部已停"

    # 核实结束：释放资源、销掉异常 → 这时才算停干净
    sched.release("T01")
    sched.resolve_incident("T01", "已确认远端终止")
    view = sched.projection()
    assert view["pause_state"]["unsettled"] == []
    assert view["pause_state"]["fully_stopped"] is True

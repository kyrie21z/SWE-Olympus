"""人工关口的 LangGraph 场景测试（issue #6 的 `tests/scenarios/gates/`）。

验证"准备 → 持久化等待 → 原子接纳"三段式，重点四条：

1. 第一次调用停在中断点，请求**已落盘**且状态 pending；
2. 恢复后完成接纳，请求状态 accepted；
3. **重放不重建请求**：再次 start 拿到同一个 request_id；
4. 内容版本变了 → 请求作废（stale），不把旧答案搬到新对象上。
"""

from __future__ import annotations

import pytest
from olympus.commands.core.bus import CommandBus
from olympus.contracts.models import Command, ContractBundle
from olympus.human.store import HumanRequestStore
from olympus.orchestrator.scheduler import Scheduler
from olympus.orchestrator.state_machine import RunContext
from olympus.planning.service import PlanService
from olympus.workflow.gates import Gate


@pytest.fixture
def world(bundle: ContractBundle, tmp_path):
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    ctx = RunContext(run=bundle.run)
    sched = Scheduler(plan=bundle.plan, manifest=package.manifest, ctx=ctx)
    bus = CommandBus(ctx=ctx, scheduler=sched)
    store = HumanRequestStore(tmp_path / "human_requests.json")
    return ctx, bus, store, bundle


def test_gate_stops_at_interrupt_and_persists_request(world):
    """第一次调用：停在中断点，请求落盘且 pending。"""
    ctx, bus, store, bundle = world
    gate = Gate(bus, store)

    result = gate.start(kind="plan_approval", target_id=bundle.plan.object_id,
                        expected_version=bundle.plan.version)

    assert result["status"] == "interrupted"
    request = result["request"]
    assert request["kind"] == "plan_approval"
    assert store.load(request["request_id"]).status == "pending"
    assert ctx.run.status == "draft", "等待期间不改 run 状态"


def test_gate_resumes_and_accepts(world):
    """恢复后完成接纳：命令被受理，请求状态 accepted。"""
    ctx, bus, store, bundle = world
    gate = Gate(bus, store)

    started = gate.start(kind="plan_approval", target_id=bundle.plan.object_id,
                         expected_version=bundle.plan.version)
    request_id = started["request"]["request_id"]

    done = gate.resume(answer={"decision": "approve", "note": "计划已复核"})

    assert done["status"] == "completed"
    assert done["receipt_status"] == "accepted"
    assert done["receipt_reason"] == "ok"
    assert store.load(request_id).status == "accepted"
    assert any("计划批准" in e.reason for e in ctx.events)


def test_gate_replay_reuses_same_request(world):
    """重放不重建请求：prepare 幂等，request_id 不变。"""
    ctx, bus, store, bundle = world
    gate = Gate(bus, store)

    first = gate.start(kind="plan_approval", target_id=bundle.plan.object_id,
                       expected_version=bundle.plan.version)
    again = store.prepare(run_id=ctx.run.run_id, kind="plan_approval",
                          target_id=bundle.plan.object_id,
                          expected_version=bundle.plan.version,
                          expected_state_revision=ctx.run.state_revision)

    assert again.request_id == first["request"]["request_id"]
    assert len([r for r in store.all() if r.kind == "plan_approval"]) == 1


def test_gate_rejects_stale_request(world):
    """内容版本变了：旧请求作废，回执 rejected/stale。"""
    ctx, bus, store, bundle = world
    gate = Gate(bus, store)

    started = gate.start(kind="plan_approval", target_id=bundle.plan.object_id,
                         expected_version=bundle.plan.version)
    request_id = started["request"]["request_id"]

    # 等待期间发生别的变更（start 让 run 状态与 revision 前移）
    bus.dispatch(Command(operation_id="op-start", run_id=ctx.run.run_id, actor="YOU",
                         action="start", target_id=ctx.run.run_id,
                         expected_version=bundle.plan.version,
                         expected_state_revision=ctx.run.state_revision, parameters={}))
    assert ctx.run.state_revision > 0

    done = gate.resume(answer={"decision": "approve"})

    assert done["receipt_status"] == "rejected"
    assert done["receipt_reason"] == "stale"
    assert store.load(request_id).status == "stale"


def test_gate_reject_decision(world):
    """驳回：走 reject_plan，run 仍为 draft。"""
    ctx, bus, store, bundle = world
    gate = Gate(bus, store)

    started = gate.start(kind="plan_approval", target_id=bundle.plan.object_id,
                         expected_version=bundle.plan.version)
    done = gate.resume(answer={"decision": "reject", "note": "任务粒度太粗，重拆"})

    assert done["status"] == "completed"
    # 契约的 Command.action 没有"驳回"，所以驳回不产生命令，只记录事件
    assert done["receipt_status"] == "rejected"
    assert done["receipt_reason"] == "rejected_by_human"
    assert ctx.run.status == "draft", "驳回后计划仍需重做"
    assert any("驳回" in e.reason for e in ctx.events)
    assert store.load(started["request"]["request_id"]).status == "accepted"

# ---------------------------------------------------------------- 中断恢复的三条验收
def test_resume_does_not_recharge(world):
    """恢复重用请求，不重复扣额度、不重复写事件。

    issue #6 原文："中断恢复重用请求，不重复扣额度/批准；同线程恢复串行。"
    """
    ctx, bus, store, bundle = world
    gate = Gate(bus, store)

    started = gate.start(kind="plan_approval", target_id=bundle.plan.object_id,
                         expected_version=bundle.plan.version)
    calls_before = ctx.run.calls_used
    revision_before = ctx.run.state_revision

    first = gate.resume(answer={"decision": "approve", "note": "同意"})
    events_after_first = len(ctx.events)

    assert first["status"] == "completed"
    assert ctx.run.calls_used == calls_before, "恢复不消耗模型调用"
    assert ctx.run.state_revision == revision_before + 1, "只前移一格"
    assert events_after_first == 1

    # 再次 resume：已完成 thread 返回最终态，不重跑节点
    second = gate.resume(answer={"decision": "approve", "note": "同意"})
    assert second["status"] == "completed"
    assert len(ctx.events) == events_after_first, "没有写第二条事件"
    assert ctx.run.state_revision == revision_before + 1, "版本没有再次前移"
    assert store.load(started["request"]["request_id"]).status == "accepted"


def test_concurrent_resume_is_serialized(world):
    """同线程并发恢复只生效一次（Gate 内部串行）。"""
    import threading

    ctx, bus, store, bundle = world
    gate = Gate(bus, store)
    gate.start(kind="plan_approval", target_id=bundle.plan.object_id,
               expected_version=bundle.plan.version)

    results: list[dict] = []
    lock = threading.Lock()

    def worker() -> None:
        out = gate.resume(answer={"decision": "approve"})
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 3
    assert all(r["status"] == "completed" for r in results), "三个调用都拿到最终态"
    assert len(ctx.events) == 1, "接纳只发生一次，事件只有一条"
    assert ctx.run.state_revision == 1


def test_new_version_requires_new_confirmation(world):
    """新稿不继承旧签认：内容一变，旧请求即失效，必须重新确认。

    issue #6 原文："修改与确认并发时旧确认拒绝，新稿不继承旧签认。"
    """
    ctx, bus, store, bundle = world
    gate = Gate(bus, store)

    started = gate.start(kind="plan_approval", target_id=bundle.plan.object_id,
                         expected_version=bundle.plan.version)
    request_id = started["request"]["request_id"]
    gate.resume(answer={"decision": "approve", "note": "同意"})
    assert store.load(request_id).status == "accepted"
    revision_after_approval = ctx.run.state_revision

    # 内容变更：新稿产生新 revision
    ctx.commit(reason="计划内容修订", actor="YOU", actor_type="human", mutate=lambda snap: None)
    assert ctx.run.state_revision == revision_after_approval + 1
    assert not store.is_current(request_id, state_revision=ctx.run.state_revision,
                                version=bundle.plan.version), "旧确认对新稿无效"

    # 新版本必须新的确认请求：旧的那张已 accepted，不会被复用
    fresh = store.prepare(run_id=ctx.run.run_id, kind="plan_approval",
                          target_id=bundle.plan.object_id,
                          expected_version=bundle.plan.version,
                          expected_state_revision=ctx.run.state_revision)
    assert fresh.request_id != request_id, "新稿不能继承旧签认"
    assert fresh.status == "pending"
    assert store.load(request_id).status == "accepted", "旧记录保留，作为历史"

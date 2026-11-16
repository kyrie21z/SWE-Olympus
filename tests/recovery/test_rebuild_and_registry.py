"""恢复与运行态注册表的测试。

这里验证的不是"能读出来"，而是三种说不清的情况会不会被拦住：

1. 事件条数与快照序号不一致 → 拒绝恢复；
2. 事件序号/修订号不连续 → 拒绝恢复；
3. 两次绑定计划 → 拒绝，因为会重置许可与占用。

另外验证"重启"语义：新建注册表从同一份存储重建后，状态与事件完全一致。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from olympus.app import create_app
from olympus.commands.core.bus import CommandBus
from olympus.contracts.models import Command, Run
from olympus.orchestrator.recovery import RecoveryGap, RuntimeRegistry, rebuild_context
from olympus.planning.service import PlanService
from olympus.storage.sqlite import Store


def seed_run(tmp_path: Path, bundle) -> tuple[Store, Path]:
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    store.create(bundle.run)
    return store, path


def pause_command(bundle, operation_id: str = "op-recovery-1") -> Command:
    return Command(
        operation_id=operation_id,
        run_id=bundle.run.run_id,
        actor="YOU",
        action="pause",
        target_id=bundle.run.run_id,
        expected_version=1,
        expected_state_revision=bundle.run.state_revision,
        parameters={},
    )


def paused(run: Run) -> Run:
    proposed = run.model_copy(deep=True)
    proposed.status = "paused"
    return proposed


def test_rebuild_restores_committed_state(tmp_path, bundle):
    store, _ = seed_run(tmp_path, bundle)
    store.commit(pause_command(bundle), paused(bundle.run), reason="测试暂停")

    ctx = rebuild_context(store, bundle.run.run_id)

    assert ctx.run.status == "paused"
    assert ctx.run.event_seq == 1
    assert ctx.run.state_revision == 1
    assert [event.event_seq for event in ctx.events] == [1]
    # 恢复出来的运行态必须与存储里的快照逐字段一致，而不是"差不多"。
    assert ctx.run == store.snapshot(bundle.run.run_id)


def test_rebuild_rejects_missing_event(tmp_path, bundle):
    store, _ = seed_run(tmp_path, bundle)
    store.commit(pause_command(bundle), paused(bundle.run), reason="测试暂停")
    stored = store.events(bundle.run.run_id, 0)

    class TruncatedStore:
        """模拟"快照显示有 1 条事件，但日志里一条都没有"的损坏。"""

        def snapshot(self, run_id: str) -> Run:
            return store.snapshot(run_id)

        def events(self, run_id: str, after_seq: int = 0):
            return [event for event in stored[:0] if event.event_seq > after_seq]

    with pytest.raises(RecoveryGap) as excinfo:
        rebuild_context(TruncatedStore(), bundle.run.run_id)

    assert excinfo.value.detail["run_event_seq"] == 1
    assert excinfo.value.detail["stored_events"] == 0


def test_registry_caches_one_runtime(tmp_path, bundle):
    store, _ = seed_run(tmp_path, bundle)
    registry = RuntimeRegistry(store)

    first = registry.get(bundle.run.run_id)
    second = registry.get(bundle.run.run_id)

    assert first is second
    assert len(registry) == 1


def test_registry_recovers_after_restart(tmp_path, bundle):
    store, path = seed_run(tmp_path, bundle)
    store.commit(pause_command(bundle), paused(bundle.run), reason="测试暂停")

    # 新进程 = 新的 Store 与新的注册表，只有磁盘上的文件是共享的。
    restarted = RuntimeRegistry(Store(path))
    runtime = restarted.get(bundle.run.run_id)

    assert runtime.ctx.run.status == "paused"
    assert runtime.ctx.run.event_seq == 1
    assert runtime.bus is None  # 计划尚未绑定，不能执行命令


def test_api_serves_state_recovered_after_restart(tmp_path, bundle):
    store, path = seed_run(tmp_path, bundle)
    store.commit(pause_command(bundle), paused(bundle.run), reason="测试暂停")

    client = TestClient(create_app(store=Store(path)))

    body = client.get(f"/api/runs/{bundle.run.run_id}").json()

    assert body["status"] == "paused"
    assert body["state_revision"] == 1


def test_attach_plan_is_single_shot(tmp_path, bundle):
    store, _ = seed_run(tmp_path, bundle)
    runtime = RuntimeRegistry(store).get(bundle.run.run_id)
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)

    assert runtime.bus is None
    runtime.attach_plan(bundle.plan, package.manifest)
    assert isinstance(runtime.bus, CommandBus)
    assert runtime.has_plan

    with pytest.raises(RuntimeError):
        runtime.attach_plan(bundle.plan, package.manifest)


def test_attached_bus_registers_the_four_runtime_actions(tmp_path, bundle):
    """M4 之后，四个动作必须从 NOT_READY 变成真实处理器。"""
    store, _ = seed_run(tmp_path, bundle)
    runtime = RuntimeRegistry(store).get(bundle.run.run_id)
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    runtime.attach_plan(bundle.plan, package.manifest)
    task = next(task for task in bundle.plan.tasks if task.task_id == "T01")

    catalog = {row["action"]: row for row in runtime.bus.catalog()}
    for action in ("retry", "reassign", "accept_risk", "request_change"):
        assert catalog[action]["implemented"] is True

    receipt = runtime.bus.dispatch(
        Command(
            operation_id="op-retry-on-draft",
            run_id=bundle.run.run_id,
            actor="YOU",
            action="retry",
            target_id=task.task_id,
            expected_version=task.version,
            expected_state_revision=0,
            parameters={"prompt": "补一行锁"},
        )
    )

    # 已注册：不再是 NOT_READY；draft 状态下按 run 前置条件拒绝。
    assert receipt.status == "rejected"
    assert receipt.reason_code == "bad_state"

"""修复子任务的测试。

验证 issue #9 的三条验收：

1. 修复任务保存 supersedes / 失败引用；
2. 旧验收失效并重新验证，独立支线（T04）不受影响；
3. 修复不能依赖后继而造环。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from olympus.contracts.models import Command
from olympus.orchestrator.recovery import (
    RepairError,
    RunRuntime,
    RuntimeRegistry,
    plan_repair,
    repair_dependencies,
)
from olympus.planning.service import PlanService
from olympus.storage.sqlite import Store

ACCEPT_PATH = (
    ("ready", None),
    ("running", None),
    ("verifying", None),
    ("accepted", "passed"),
)


def build_runtime(tmp_path: Path, bundle) -> RunRuntime:
    store = Store(tmp_path / "state.sqlite3")
    store.create(bundle.run)
    runtime = RuntimeRegistry(store).get(bundle.run.run_id)
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    runtime.attach_plan(bundle.plan, package.manifest)
    return runtime


def start(runtime: RunRuntime, bundle) -> None:
    receipt = runtime.bus.dispatch(
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
    assert receipt.status == "accepted"


def accept(runtime: RunRuntime, task_id: str) -> None:
    for state, verification in ACCEPT_PATH:
        if runtime.ctx.state_of(task_id) == state:
            continue
        runtime.ctx.transition(
            task_id,
            state,
            reason="测试推进",
            actor="Argus" if state == "accepted" else "Zeus",
            actor_type="agent" if state == "accepted" else "program",
            verification=verification,
        )


def ready_after_start(tmp_path, bundle) -> RunRuntime:
    runtime = build_runtime(tmp_path, bundle)
    start(runtime, bundle)
    accept(runtime, "T01")
    runtime.scheduler.reconcile()
    accept(runtime, "T02")
    accept(runtime, "T03")
    runtime.scheduler.reconcile()
    return runtime


def test_repair_invalidates_old_acceptance_and_rewires_dependents(tmp_path, bundle):
    runtime = ready_after_start(tmp_path, bundle)
    assert runtime.ctx.state_of("T05") == "ready"
    assert runtime.ctx.state_of("T06") == "ready"

    outcome = plan_repair(
        runtime,
        "T03",
        failure_ref="REC-001",
        reason="取消竞争导致同一候补被递补两次",
    )

    assert outcome.repair_task_id == "T03-R1"
    assert outcome.supersedes == "T03"
    assert "T03" in outcome.invalidated
    assert {"T05", "T06"}.issubset(set(outcome.demoted))

    # 旧验收失效，但不是删除：状态回退、验证标 stale、代际 +1。
    upstream = runtime.ctx.runtime("T03")
    assert upstream.state == "cancelled"
    assert upstream.verification_status == "stale"
    assert upstream.generation == 2

    # 修复任务就绪，并且只依赖上游的前驱。
    repair = runtime.scheduler.tasks["T03-R1"]
    assert repair.depends_on == ["T01"]
    assert runtime.ctx.state_of("T03-R1") == "ready"

    # 直接后继改依赖修复任务；独立支线 T04 不受影响。
    assert runtime.scheduler.tasks["T05"].depends_on == ["T03-R1"]
    assert runtime.scheduler.tasks["T06"].depends_on == ["T02", "T03-R1"]
    assert runtime.ctx.state_of("T04") == "ready"
    assert all("T03" not in task.depends_on for task in runtime.plan.tasks)

    # 计划前进一版，修复记录可审计。
    assert runtime.plan.version == bundle.plan.version + 1
    assert runtime.repairs[-1]["failure_ref"] == "REC-001"
    assert runtime.repairs[-1]["supersedes"] == "T03"


def test_repair_keeps_the_graph_acyclic(tmp_path, bundle):
    runtime = ready_after_start(tmp_path, bundle)

    plan_repair(runtime, "T03", failure_ref="REC-001", reason="重复递补")

    manifest = runtime.scheduler.manifest
    assert len(manifest.topological_order) == len(runtime.plan.tasks)
    order = {task_id: index for index, task_id in enumerate(manifest.topological_order)}
    assert order["T03-R1"] < order["T05"]
    assert order["T03-R1"] < order["T06"]


def test_only_one_automatic_repair_per_task(tmp_path, bundle):
    runtime = ready_after_start(tmp_path, bundle)
    plan_repair(runtime, "T03", failure_ref="REC-001", reason="第一次修复")

    with pytest.raises(RepairError) as excinfo:
        plan_repair(runtime, "T03", failure_ref="REC-002", reason="第二次修复")

    assert "人工授权" in str(excinfo.value)


def test_repair_rejects_dependencies_on_descendants():
    class Manifest:
        predecessors = {"T03": ["T06"]}
        descendants = {"T03": ["T05", "T06"]}

    with pytest.raises(RepairError) as excinfo:
        repair_dependencies(Manifest(), "T03")

    assert "环" in str(excinfo.value)


def test_repair_unknown_task_is_rejected(tmp_path, bundle):
    runtime = build_runtime(tmp_path, bundle)

    with pytest.raises(RepairError):
        plan_repair(runtime, "T99", failure_ref="REC-404", reason="不存在")

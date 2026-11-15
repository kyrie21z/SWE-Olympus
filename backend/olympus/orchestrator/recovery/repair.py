"""上游修复子任务（`T03-R1`）的创建与图改写。

一条修复要同时改五样东西，少一样就不一致：

1. `Run.tasks` 里追加一个 `TaskRuntime`（新任务要能被调度器看到）；
2. `Plan` 前进一版，加入修复任务；
3. 直接把修复任务的后继改成依赖修复任务（原任务被取代）；
4. 旧验收失效：`accepted → waiting`、`stale`、代际 +1；
5. 调度器的 `plan` / `manifest` 视图换新。

**前驱怎么选**：取上游任务自己的前驱，绝不取它的后继。
否则 `T03-R1 ← T06 ← T05 ← T03` 就是环，issue 明确禁止。
这里用 `repair_dependencies` 做显式检查，不靠"看图应该没问题"。

**为什么改后继的 depends_on**：修复任务取代原任务后，原来等 `T03` 的任务
必须改等 `T03-R1`。这不是"重算拓扑"——只做一次局部替换，拓扑仍由
P2 的编译器重算。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from olympus.contracts.models import Plan, Run, Task, TaskRuntime, fingerprint
from olympus.orchestrator.recovery.registry import RunRuntime
from olympus.orchestrator.state_machine.revision import affected_closure, supersede_plan


class RepairError(ValueError):
    """无法安全创建修复任务：重复修复、依赖成环或状态不允许。"""


@dataclass(frozen=True)
class RepairOutcome:
    repair_task_id: str
    supersedes: str
    plan_version: int
    invalidated: tuple[str, ...]
    demoted: tuple[str, ...]
    rewired: dict[str, list[str]]
    ready: list[str]


def repair_dependencies(manifest, upstream_task_id: str) -> list[str]:
    """修复任务的前驱 = 上游任务的前驱；并显式排除自身与全部后继。"""
    deps = list(manifest.predecessors.get(upstream_task_id, []))
    forbidden = {upstream_task_id, *manifest.descendants.get(upstream_task_id, [])}
    overlap = sorted(forbidden.intersection(deps))
    if overlap:
        raise RepairError(
            f"{upstream_task_id} 的修复任务不能依赖自身或后继：{overlap}（会形成环）"
        )
    return deps


def plan_repair(
    runtime: RunRuntime,
    upstream_task_id: str,
    *,
    failure_ref: str,
    reason: str,
    allow_additional: bool = False,
) -> RepairOutcome:
    if runtime.scheduler is None or runtime.plan is None:
        raise RepairError("运行尚未绑定计划，不能创建修复任务")
    ctx = runtime.ctx
    plan = runtime.plan
    manifest = runtime.scheduler.manifest

    upstream = next((task for task in plan.tasks if task.task_id == upstream_task_id), None)
    if upstream is None:
        raise RepairError(f"计划中不存在任务 {upstream_task_id}")

    existing = [task.task_id for task in plan.tasks if task.task_id.startswith(f"{upstream_task_id}-R")]
    if existing and not allow_additional:
        raise RepairError(
            f"{upstream_task_id} 已有修复任务 {existing}；额外修复需人工授权（allow_additional）"
        )

    repair_id = f"{upstream_task_id}-R{len(existing) + 1}"
    deps = repair_dependencies(manifest, upstream_task_id)

    # 1) 旧验收失效：accepted → waiting，标 stale，代际 +1。
    #    这里没有直接用 P3 的 invalidate_revision，因为它把 actor_type 固定写成 human；
    #    自动修复是程序动作，事件类型必须如实。
    invalidated: list[str] = []
    for task_id in affected_closure(manifest, [upstream_task_id]):
        if ctx.state_of(task_id) != "accepted":
            continue

        def mutate(snapshot: Run, tid: str = task_id) -> None:
            item = next(t for t in snapshot.tasks if t.task_id == tid)
            item.state = "waiting"                 # 先离开 accepted
            item.verification_status = "stale"     # 再标过期
            item.generation += 1

        ctx.commit(
            reason=f"修复失效：{upstream_task_id} 将由 {repair_id} 取代",
            actor="Zeus",
            actor_type="program",
            mutate=mutate,
            task_id=task_id,
        )
        invalidated.append(task_id)

    # 2) 前驱不再全部验收的 ready 任务降回 waiting，避免界面显示"可以开工"。
    demoted: list[str] = []
    for task in plan.tasks:
        if ctx.state_of(task.task_id) != "ready":
            continue
        predecessors = manifest.predecessors.get(task.task_id, [])
        if all(ctx.state_of(p) == "accepted" for p in predecessors):
            continue
        ctx.transition(
            task.task_id,
            "waiting",
            reason=f"前驱因修复失效：{upstream_task_id}",
            actor="Zeus",
            actor_type="program",
        )
        demoted.append(task.task_id)

    # 3) 原任务转终态，表示已被取代（历史证据保留）。
    if ctx.state_of(upstream_task_id) not in ("cancelled", "accepted"):
        ctx.transition(
            upstream_task_id,
            "cancelled",
            reason=f"已被 {repair_id} 取代",
            actor="Zeus",
            actor_type="program",
        )

    # 4) 追加运行时任务；事件记录这次计划变更。
    ctx.commit(
        reason=f"创建修复任务 {repair_id}（supersedes {upstream_task_id}，失败引用 {failure_ref}）",
        actor="Zeus",
        actor_type="program",
        mutate=lambda snapshot: snapshot.tasks.append(TaskRuntime(task_id=repair_id)),
    )

    # 5) 生成修复任务卡；语义字段继承上游，前驱换成上游的前驱。
    repair_task = _build_repair_task(upstream, repair_id, deps, reason)
    new_plan, rewired = _rewire_dependents(plan, upstream_task_id, repair_id, repair_task)

    # 6) 重算 manifest 并替换调度器视图（不动许可、尝试与代际记录）。
    from olympus.planning.compiler import compile_plan

    new_manifest = compile_plan(new_plan)
    runtime.plan = new_plan
    runtime.scheduler.plan = new_plan
    runtime.scheduler.manifest = new_manifest
    runtime.repairs.append(
        {
            "repair_task_id": repair_id,
            "supersedes": upstream_task_id,
            "failure_ref": failure_ref,
            "reason": reason,
            "plan_version": new_plan.version,
            "at": datetime.now(UTC).isoformat(),
        }
    )

    ready = runtime.scheduler.reconcile()
    return RepairOutcome(
        repair_task_id=repair_id,
        supersedes=upstream_task_id,
        plan_version=new_plan.version,
        invalidated=tuple(invalidated),
        demoted=tuple(demoted),
        rewired=rewired,
        ready=ready,
    )


def _build_repair_task(upstream: Task, repair_id: str, deps: list[str], reason: str) -> Task:
    """语义字段继承上游，只改编号、目标、前驱和版本。"""
    payload = {
        "task_id": repair_id,
        "type": upstream.type,
        "goal": f"修复 {upstream.task_id}：{reason}"[:160],
        "sources": list(upstream.sources),
        "inputs": list(upstream.inputs),
        "outputs": list(upstream.outputs),
        "depends_on": list(deps),
        "executor": upstream.executor,
        "reviewer": upstream.reviewer,
        "write_scope": list(upstream.write_scope),
        "checks": list(upstream.checks),
        "resources": list(upstream.resources),
        "priority": upstream.priority,
        "policy": upstream.policy.model_dump(mode="json"),
        "required": upstream.required,
    }
    return Task(version=1, content_hash=fingerprint(payload), **payload)


def _rewire_dependents(
    plan: Plan, upstream_task_id: str, repair_id: str, repair_task: Task
) -> tuple[Plan, dict[str, list[str]]]:
    """把直接依赖原任务的任务改成依赖修复任务；版本 +1，指纹重算。"""
    rewired: dict[str, list[str]] = {}
    tasks: list[Task] = []

    for task in plan.tasks:
        if task.task_id == upstream_task_id:
            tasks.append(task)
            continue
        if upstream_task_id not in task.depends_on:
            tasks.append(task)
            continue

        payload = task.semantic_content()
        new_deps = [repair_id if dep == upstream_task_id else dep for dep in payload["depends_on"]]
        payload["depends_on"] = new_deps
        tasks.append(
            Task(version=task.version + 1, content_hash=fingerprint(payload), **payload)
        )
        rewired[task.task_id] = new_deps

    tasks.append(repair_task)
    new_content = {"tasks": [task.model_dump(mode="json") for task in tasks]}
    return supersede_plan(plan, new_content), rewired

"""运行态投影（issue #5 要求的 `backend/olympus/projections/runtime/`）。

只做一件事：**把"现在能派什么、卡在哪、验收了几个"算成一个可直接序列化的快照**，
让 P5 包 HTTP、P6 画界面时不用再实现任何业务判据。

四条硬要求（对应 issue 验收条件）：

| 要求 | 实现 |
| --- | --- |
| 状态摘要来自权威记录 | 每项都从 `RunContext.run` 与 `manifest` 现算，不存第二份 |
| accepted n/N 只算必要任务 | `progress.required` 只取 `task.required` |
| 等待/阻塞原因可读 | `sets.blocked[].why` 是中文短句，界面直接显示 |
| 动态资源占用可查 | `resources.inflight / executors / permits` |

不在这里做的事：不判断"该不该派"（那是 `Scheduler.dispatchable`），
不算静态拓扑（那是 P2 的 manifest），不给建议（那是 YOU 的事）。
"""

from __future__ import annotations

from typing import Any, Protocol


class _SchedulerLike(Protocol):
    """只依赖 Scheduler 的公开接口，避免与调度器循环导入。"""

    plan: Any
    manifest: Any
    ctx: Any

    def eligible(self) -> list[str]: ...
    def ready(self) -> list[str]: ...
    def dispatchable(self) -> list[tuple[str, list[str]]]: ...
    def rank(self, task_id: str) -> int: ...


def build_runtime_projection(scheduler: _SchedulerLike) -> dict:
    ctx = scheduler.ctx
    plan = scheduler.plan
    manifest = scheduler.manifest
    disp = scheduler.dispatchable()

    required = [t for t in plan.tasks if t.required]
    accepted = [t for t in required if ctx.state_of(t.task_id) == "accepted"]

    permits = getattr(scheduler, "_permits", {})
    executor_load = getattr(scheduler, "_executor_load", {})

    return {
        "run": ctx.snapshot_view(),
        "manifest": {
            "manifest_id": manifest.manifest_id,
            "layers": manifest.layers,
            "topological_order": manifest.topological_order,
            "critical_chain": manifest.critical_path.critical_chain,
        },
        "sets": {
            "eligible": scheduler.eligible(),
            "ready": scheduler.ready(),
            "dispatchable": [tid for tid, why in disp if not why],
            "blocked": [{"task_id": tid, "why": why} for tid, why in disp if why],
        },
        "progress": {"accepted": len(accepted), "required": len(required)},
        "pause_state": {
            "paused": ctx.run.status == "paused",
            # 只有"没有未核实的在途"时才是真的停干净
            "fully_stopped": ctx.run.status == "paused" and not _unsettled(scheduler),
            "unsettled": _unsettled(scheduler),
        },
        "incidents": {
            "open": [i.to_dict() for i in _incidents(scheduler).open()],
            "resolved": _incidents(scheduler).resolved(),
        },
        "resources": {
            "inflight": len(permits),
            "max_parallel": getattr(scheduler, "max_parallel", 0),
            "executor_capacity": getattr(scheduler, "executor_capacity", 0),
            "executors": dict(executor_load),
            "permits": [
                {
                    "task_id": p.task_id,
                    "generation": p.generation,
                    "executor": p.executor,
                    "write_scope": list(p.write_scope),
                    "resources": list(p.resources),
                }
                for p in permits.values()
            ],
            "attempts": dict(getattr(scheduler, "_attempts", {})),
        },
        "tasks": [
            {
                "task_id": t.task_id,
                "type": t.type,
                "state": ctx.state_of(t.task_id),
                "verification": ctx.runtime(t.task_id).verification_status,
                "generation": ctx.runtime(t.task_id).generation,
                "priority_rank": scheduler.rank(t.task_id),
                "executor": t.executor,
                "reviewer": t.reviewer,
                "required": t.required,
                "missing_predecessors": [
                    p for p in manifest.predecessors.get(t.task_id, [])
                    if ctx.state_of(p) != "accepted"
                ],
            }
            for t in sorted(plan.tasks, key=lambda x: (scheduler.rank(x.task_id), x.task_id))
        ],
    }


def _unsettled(scheduler: Any) -> list[str]:
    fn = getattr(scheduler, "unsettled_inflight", None)
    return fn() if callable(fn) else []


def _incidents(scheduler: Any):
    """异常登记簿可能是空的（早期构造），退回一个空账本避免投影崩。"""
    ledger = getattr(scheduler, "_incidents", None)
    if ledger is not None:
        return ledger

    class _Empty:
        def open(self):
            return []

        def resolved(self):
            return {}

    return _Empty()
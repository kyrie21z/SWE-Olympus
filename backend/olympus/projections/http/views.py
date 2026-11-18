"""HTTP 投影组装层。

这一层只做**拼装**，不做判断：

- 运行状态摘要与 `allowed_actions` 直接取 P3；
- 追踪矩阵直接取 P2；
- 执行与工件详情由注入的 P4 查询服务提供。

API 不重新计算"能不能做""是否已验收"，避免出现第二套判据。
"""

from __future__ import annotations

from typing import Protocol

from olympus.contracts.models import fingerprint
from olympus.orchestrator.recovery import RunRuntime


class ExecutionView(Protocol):
    """P4 查询服务的窄接口：`ArtifactAcceptanceService.project` 即可满足。"""

    def project(self, task_id: str, attempt_id: str | None = None) -> dict: ...


def build_run_view(runtime: RunRuntime) -> dict:
    """一次请求返回前端需要的全部只读视图。"""
    if runtime.scheduler is None or runtime.bus is None:
        raise ValueError("运行尚未绑定计划")

    with runtime.lock, runtime.scheduler._lock:
        return {
            "schema_version": "1.0",
            "run_id": runtime.run_id,
            "snapshot": runtime.ctx.run.model_dump(mode="json"),
            "projection": runtime.scheduler.projection(),
            "plan": runtime.scheduler.plan.model_dump(mode="json"),
            "allowed_actions": runtime.bus.allowed_actions("YOU"),
            "trace": build_trace_view(runtime),
            "repairs": list(runtime.repairs),
        }


def build_trace_view(runtime: RunRuntime) -> dict | None:
    if runtime.trace is None:
        return None
    # P2's planning index stays immutable and unverified. This HTTP projection
    # overlays only authoritative Zeus task states; it never accepts a task.
    with runtime.lock:
        trace = runtime.trace.model_dump(mode="json")
        states = {task.task_id: task for task in runtime.ctx.run.tasks}
        for row in trace["rows"]:
            tasks = [states.get(task_id) for task_id in row["all_tasks"]]
            verified = bool(tasks) and bool(row["test_tasks"]) and all(
                task is not None and task.state == "accepted" and task.verification_status == "passed"
                and task.disposition != "risk_accepted" for task in tasks
            )
            row["accepted"] = verified
            row["verification_status"] = "passed" if verified else (
                "stale" if any(task and task.verification_status == "stale" for task in tasks) else
                "failed" if any(task and task.verification_status == "failed" for task in tasks) else "unverified"
            )
        trace["planning_index_hash"] = runtime.trace.index_hash
        trace["index_hash"] = fingerprint({key: value for key, value in trace.items()
                                           if key not in {"schema_version", "index_hash"}})
        return trace

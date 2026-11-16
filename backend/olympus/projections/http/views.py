"""HTTP 投影组装层。

这一层只做**拼装**，不做判断：

- 运行状态摘要与 `allowed_actions` 直接取 P3；
- 追踪矩阵直接取 P2；
- 执行与工件详情由注入的 P4 查询服务提供。

API 不重新计算"能不能做""是否已验收"，避免出现第二套判据。
"""

from __future__ import annotations

from typing import Protocol

from olympus.orchestrator.recovery import RunRuntime


class ExecutionView(Protocol):
    """P4 查询服务的窄接口：`ArtifactAcceptanceService.project` 即可满足。"""

    def project(self, task_id: str, attempt_id: str | None = None) -> dict: ...


def build_run_view(runtime: RunRuntime) -> dict:
    """一次请求返回前端需要的全部只读视图。"""
    if runtime.scheduler is None or runtime.bus is None:
        raise ValueError("运行尚未绑定计划")

    return {
        "schema_version": "1.0",
        "run_id": runtime.run_id,
        "snapshot": runtime.ctx.run.model_dump(mode="json"),
        "projection": runtime.scheduler.projection(),
        "allowed_actions": runtime.bus.allowed_actions("YOU"),
        "trace": runtime.trace.model_dump(mode="json") if runtime.trace is not None else None,
        "repairs": list(runtime.repairs),
    }


def build_trace_view(runtime: RunRuntime) -> dict | None:
    return runtime.trace.model_dump(mode="json") if runtime.trace is not None else None

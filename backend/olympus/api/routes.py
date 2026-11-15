"""只读路由。

路径按 `OlympusClient`（`frontend/src/data/core/client.ts`）的三个方法安排：
- `getSnapshot(runId)`  → `GET /api/runs/{run_id}`
- `getEvents(runId, n)` → `GET /api/runs/{run_id}/events?after_seq=n`
- `submit(command)`     → `POST /api/commands`（M2 接入，本阶段不提供）

当前只做读取，不提供任何写入口，因此不存在"点了按钮就乐观标成成功"的可能。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from olympus.api.service import RunQueryService, build_service
from olympus.contracts.models import Event, Run

router = APIRouter(prefix="/api", tags=["runs"])


def get_runs(request: Request) -> RunQueryService:
    """从应用状态取服务实例；测试可以注入临时数据库，不依赖全局单例。"""
    runs = getattr(request.app.state, "runs", None)
    if runs is None:
        runs = build_service()
        request.app.state.runs = runs
        request.app.state.store = runs.store
    return runs


@router.get("/runs/{run_id}", response_model=Run)
def read_snapshot(run_id: str, runs: RunQueryService = Depends(get_runs)) -> Run:
    return runs.snapshot(run_id)


@router.get("/runs/{run_id}/events", response_model=list[Event])
def read_events(
    run_id: str,
    after_seq: int = Query(0, description="只返回 event_seq 大于该值的事件；0 表示从头补取"),
    runs: RunQueryService = Depends(get_runs),
) -> list[Event]:
    return runs.events(run_id, after_seq)

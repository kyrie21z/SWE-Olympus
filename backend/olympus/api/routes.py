"""HTTP 路由。

路径按 `OlympusClient`（`frontend/src/data/core/client.ts`）的三个方法安排：
- `getSnapshot(runId)`  → `GET  /api/runs/{run_id}`
- `getEvents(runId, n)` → `GET  /api/runs/{run_id}/events?after_seq=n`
- `submit(command)`     → `POST /api/commands`

写入口只有一个，且**必须携带 durability 声明**：调用方能从响应头看出这次
操作是否已经落库，不需要去读文档猜。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response

from olympus.api.service import RunQueryService, build_service
from olympus.commands.runtime import CommandService
from olympus.contracts.models import Command, Event, Receipt, Run

router = APIRouter(prefix="/api", tags=["runs"])


def get_runs(request: Request) -> RunQueryService:
    """从应用状态取服务实例；测试可以注入临时数据库，不依赖全局单例。"""
    runs = getattr(request.app.state, "runs", None)
    if runs is None:
        runs = build_service()
        request.app.state.runs = runs
        request.app.state.store = runs.store
    return runs


def get_commands(request: Request) -> CommandService:
    """命令服务与查询服务共用同一个运行态注册表。"""
    commands = getattr(request.app.state, "commands", None)
    if commands is None:
        commands = CommandService(get_runs(request).registry)
        request.app.state.commands = commands
    return commands


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


@router.post("/commands", response_model=Receipt)
def submit_command(
    command: Command,
    response: Response,
    commands: CommandService = Depends(get_commands),
) -> Receipt:
    outcome = commands.submit(command)
    response.headers["X-Olympus-Durability"] = outcome.durability
    response.headers["X-Olympus-Recorded-Events"] = str(outcome.recorded_events)
    return outcome.receipt


@router.get("/capabilities")
def capabilities(commands: CommandService = Depends(get_commands)) -> dict:
    return {
        "schema_version": "1.0",
        "durability": commands.durability,
        "command_journal": commands.journal_name,
        "recovery": {"mode": "single-process", "strategy": "events+snapshot"},
        "notes": [
            "命令只作用于进程内运行态；重启后回退到最近一次已提交快照",
            "多事件持久化待存储层提供批量写入接口后接入 StoreJournal",
            "恢复要求事件链与快照逐条对得上，否则返回 RECOVERY_GAP",
        ],
    }

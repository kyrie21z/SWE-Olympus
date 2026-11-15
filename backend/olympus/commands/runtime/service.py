"""命令服务：把"取运行态 → 执行命令 → 记录事件"串成一条明确路径。

职责边界：

- 计划是否绑定、命令是否合法，由 P3 的 `CommandBus` 判断，本层不重复；
- 事件记不记录、记录到哪，由 `CommandJournal` 决定，本层不假设已落库；
- 未提供 journal 时使用 `NullJournal`，并把 durability 如实回报给调用方。
"""

from __future__ import annotations

from dataclasses import dataclass

from olympus.api.errors import RunNotFound
from olympus.commands.runtime.errors import PlanNotBound
from olympus.commands.runtime.journal import CommandJournal, NullJournal
from olympus.contracts.models import Command, Receipt
from olympus.orchestrator.recovery import RuntimeRegistry


@dataclass(frozen=True)
class CommandOutcome:
    """一次命令提交的结果，包含"有没有落库"这一事实。"""

    receipt: Receipt
    durability: str
    recorded_events: int


class CommandService:
    def __init__(self, registry: RuntimeRegistry, journal: CommandJournal | None = None) -> None:
        self._registry = registry
        self._journal: CommandJournal = journal if journal is not None else NullJournal()

    @property
    def durability(self) -> str:
        return self._journal.durability

    @property
    def journal_name(self) -> str:
        return self._journal.name

    def submit(self, command: Command) -> CommandOutcome:
        try:
            runtime = self._registry.get(command.run_id)
        except KeyError as exc:
            raise RunNotFound(f"运行 {command.run_id} 不存在") from exc

        if runtime.bus is None:
            # 没有计划就没有调度器：此时执行任何命令都无法证明其前置状态。
            raise PlanNotBound(
                "运行尚未绑定已批准的计划，不能执行命令",
                detail={"run_id": command.run_id},
            )

        before = len(runtime.ctx.events)
        receipt = runtime.bus.dispatch(command)

        recorded = 0
        if receipt.status == "accepted":
            recorded = self._journal.record(runtime.ctx.events[before:], command)

        return CommandOutcome(
            receipt=receipt,
            durability=self._journal.durability,
            recorded_events=recorded,
        )

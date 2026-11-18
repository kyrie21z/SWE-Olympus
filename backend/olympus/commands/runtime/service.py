"""命令服务：把"取运行态 → 执行命令 → 记录事件"串成一条明确路径。

职责边界：

- 计划是否绑定、命令是否合法，由 P3 的 `CommandBus` 判断，本层不重复；
- 事件记不记录、记录到哪，由 `CommandJournal` 决定，本层不假设已落库；
- 未提供 journal 时使用 `NullJournal`，并把 durability 如实回报给调用方。
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext

from olympus.api.errors import RunNotFound
from olympus.commands.runtime.errors import CommandPersistenceUnavailable, CommandStateConflict, PlanNotBound
from olympus.commands.runtime.journal import CommandJournal, NullJournal, StoreJournal
from olympus.contracts.models import Command, Receipt
from olympus.orchestrator.recovery import RuntimeRegistry
from olympus.storage.sqlite import ConcurrentCommit, IdempotencyConflict


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

        with runtime.lock, runtime.bus._lock:
            with runtime.scheduler._lock if runtime.scheduler is not None else nullcontext():
                if isinstance(self._journal, StoreJournal):
                    return self._submit_durable(runtime, command)
                before = len(runtime.ctx.events)
                receipt = runtime.bus.dispatch(command)
                recorded = 0
                if receipt.status == "accepted":
                    recorded = self._journal.record(runtime.ctx.events[before:], command)
                return CommandOutcome(receipt, self.durability, recorded)

    def _submit_durable(self, runtime, command: Command) -> CommandOutcome:
        journal = self._journal
        try:
            persisted = journal.store.snapshot(runtime.run_id)
            if persisted != runtime.ctx.run:
                # The executor may have recorded budget/verifying events while
                # waiting for Argus. Flush them under the same scheduler lock
                # before establishing the command's compare-and-swap baseline.
                runtime.persist(expected=persisted)
        except ConcurrentCommit as exc:
            self._registry.forget(runtime.run_id)
            raise CommandStateConflict("运行已被其他请求更新，请刷新后重试") from exc
        except Exception as exc:
            raise CommandPersistenceUnavailable("运行进度未能持久化，此命令尚未执行") from exc
        try:
            previous = journal.lookup(command)
        except IdempotencyConflict:
            return CommandOutcome(
                Receipt(
                    operation_id=command.operation_id, run_id=command.run_id,
                    status="rejected", reason_code="conflict",
                    state_revision=runtime.ctx.run.state_revision,
                ), self.durability, 0,
            )
        if previous is not None:
            return CommandOutcome(previous, self.durability, 0)
        checkpoint = runtime.checkpoint()
        baseline = checkpoint["run"]
        before = len(runtime.ctx.events)
        try:
            receipt = runtime.bus.dispatch(command)
            if receipt.status != "accepted":
                # A failed handler must not leave a partial in-memory transition.
                runtime.restore_checkpoint(checkpoint)
                receipt = receipt.model_copy(update={"state_revision": baseline.state_revision})
            events = runtime.ctx.events[before:]
            committed, recorded = journal.commit(
                events, command, receipt, expected=baseline, runtime_state=runtime.export_state(),
            )
            if committed != receipt or (events and recorded == 0):
                # A second process won the operation race; discard our speculative effects.
                runtime.restore_checkpoint(checkpoint)
                self._registry.forget(runtime.run_id)
            return CommandOutcome(committed, self.durability, recorded)
        except IdempotencyConflict:
            runtime.restore_checkpoint(checkpoint)
            self._registry.forget(runtime.run_id)
            return CommandOutcome(
                Receipt(
                    operation_id=command.operation_id, run_id=command.run_id,
                    status="rejected", reason_code="conflict", state_revision=baseline.state_revision,
                ), self.durability, 0,
            )
        except ConcurrentCommit as exc:
            runtime.restore_checkpoint(checkpoint)
            self._registry.forget(runtime.run_id)
            raise CommandStateConflict("运行已被其他请求更新，请刷新后重试") from exc
        except Exception as exc:
            runtime.restore_checkpoint(checkpoint)
            raise CommandPersistenceUnavailable("命令未能持久化，状态已回滚，请稍后重试") from exc

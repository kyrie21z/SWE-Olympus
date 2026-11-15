"""Run 快照与受控状态迁移（P3 · OL-05 的底座）。

设计约束（来自 `olympus.contracts.models`）：

1. `Run` 是权威投影：`state_revision` 是内容版本，`event_seq` 是事件序号，两者都由程序递增，
   模型与命令都不能直接写。
2. 每条 `Event` 必须携带**同一事务里的 Run 快照**，且三元组 `(run_id, event_seq, state_revision)`
   与快照一致；`Event.matches_snapshot` 会校验。所以迁移只能有一个入口：`RunContext.commit`。
3. `TaskRuntime` 开了 `validate_assignment`，因此 `state="accepted"` 会强制检查
   `verification_status == "passed"`；我们不需要重复实现这条不变量。

这三条合起来的意思是：**没有第二条修改状态的路径**。任何绕过 `commit` 的写入都会在 pydantic
校验处失败，而不是在评审时被人工发现。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from olympus.contracts.models import Event, Run, TaskRuntime

from .transitions import assert_transition

ActorType = Literal["program", "agent", "human"]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class RunContext:
    """持有当前 Run 快照与事件序列；所有状态迁移经 `commit`。"""

    run: Run
    events: list[Event] = field(default_factory=list)
    _seq: int = 0

    # ---------------------------------------------------------------- 读
    def runtime(self, task_id: str) -> TaskRuntime:
        for item in self.run.tasks:
            if item.task_id == task_id:
                return item
        raise KeyError(f"unknown task {task_id}")

    def state_of(self, task_id: str) -> str:
        return self.runtime(task_id).state

    @property
    def accepted_required(self) -> int:
        return sum(1 for t in self.run.tasks if t.state == "accepted")

    # ---------------------------------------------------------------- 写
    def commit(
        self,
        *,
        reason: str,
        actor: str,
        actor_type: ActorType,
        mutate: Callable[[Run], None],
        task_id: str | None = None,
        attempt_id: str | None = None,
        bump_revision: bool = True,
    ) -> Event:
        """在一个不可分割的步骤里：改快照 → 递增编号 → 生成事件。

        `bump_revision=False` 用于**签认类动作**（批准/驳回）：它产生事件但不改内容，
        按契约"自身签名不递增"。否则一次批准就会让刚绑定的请求立刻过期。
        """
        snapshot = self.run.model_copy(deep=True)
        old_state = None
        if task_id is not None:
            runtime = next((t for t in snapshot.tasks if t.task_id == task_id), None)
            if runtime is None:
                raise KeyError(f"unknown task {task_id}")
            old_state = runtime.state

        mutate(snapshot)
        if bump_revision:
            snapshot.state_revision += 1
        snapshot.event_seq += 1

        self._seq += 1
        new_state = None
        if task_id is not None:
            new_state = next(t for t in snapshot.tasks if t.task_id == task_id).state

        event = Event(
            event_id=f"{snapshot.run_id}-E{snapshot.event_seq:04d}-{self._seq:03d}",
            run_id=snapshot.run_id,
            event_seq=snapshot.event_seq,
            task_id=task_id,
            attempt_id=attempt_id,
            old_state=old_state,
            new_state=new_state,
            reason=reason[:160] or "state change",
            actor_type=actor_type,
            occurred_at=_now(),
            state_revision=snapshot.state_revision,
            snapshot=snapshot,
        )
        self.run = snapshot
        self.events.append(event)
        return event

    def transition(
        self,
        task_id: str,
        new_state: str,
        *,
        reason: str,
        actor: str,
        actor_type: ActorType = "program",
        verification: str | None = None,
    ) -> Event:
        """改单个任务状态；可同时更新 verification_status（两者必须自洽）。"""

        def mutate(snapshot: Run) -> None:
            runtime = next(t for t in snapshot.tasks if t.task_id == task_id)
            assert_transition(task_id, runtime.state, new_state)   # 合法边先校验
            if verification is not None:
                runtime.verification_status = verification  # 先改验证态，再改状态
            runtime.state = new_state

        return self.commit(reason=reason, actor=actor, actor_type=actor_type,
                           mutate=mutate, task_id=task_id)

    def set_run_status(self, status: str, *, reason: str, actor: str,
                       actor_type: ActorType = "human") -> Event:
        def mutate(snapshot: Run) -> None:
            snapshot.status = status

        return self.commit(reason=reason, actor=actor, actor_type=actor_type, mutate=mutate)

    # ---------------------------------------------------------------- 投影
    def snapshot_view(self) -> dict:
        return {
            "run_id": self.run.run_id,
            "status": self.run.status,
            "state_revision": self.run.state_revision,
            "event_seq": self.run.event_seq,
            "call_budget": self.run.call_budget,
            "calls_used": self.run.calls_used,
            "tasks": [
                {
                    "task_id": t.task_id,
                    "state": t.state,
                    "verification": t.verification_status,
                    "disposition": t.disposition,
                    "generation": t.generation,
                }
                for t in self.run.tasks
            ],
        }

"""假执行器：在没有真实工具时验证"发送许可 / 代际 / 结果未知 / 迟到回执"。

为什么需要它（issue #5 明写）：

> 发送许可、执行代际、资源保留及旧回执拒绝，提供 **mock/fake executor 的查询场景**，
> P5 不再补造这些底层能力。

也就是说，P3 必须证明"一个任务派出去之后，系统怎么知道它到底成没成"——
在没有真实执行器的阶段，只能靠一个行为可控的替身。这个替身不是玩具：

- `dispatch()` 返回 `attempt_id` 与 `generation`，调用方拿它去查询；
- `query()` 可以是 `running / succeeded / failed / outcome_unknown` 四种之一；
- 只有 `commit()` 时带着**当前代际**的产物才算数，旧代际一律判 `stale`。

真实执行器（P4 的 OL-08）接入时，只要实现同样三个方法即可替换。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AttemptState = Literal["running", "succeeded", "failed", "outcome_unknown", "cancelled"]


@dataclass
class FakeAttempt:
    attempt_id: str
    task_id: str
    generation: int
    state: AttemptState = "running"
    artifacts: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    cancel_confirmed: bool = False


class FakeExecutor:
    """脚本化执行器。默认所有任务返回 `running`，按需注入成功/失败/未知。"""

    def __init__(self, script: dict[str, AttemptState] | None = None) -> None:
        self._script = dict(script or {})
        self._attempts: dict[str, FakeAttempt] = {}
        self._seq = 0

    # ---------------------------------------------------------------- 派发
    def dispatch(self, task_id: str, generation: int) -> FakeAttempt:
        self._seq += 1
        attempt = FakeAttempt(
            attempt_id=f"ATT-{task_id}-{generation}-{self._seq:02d}",
            task_id=task_id,
            generation=generation,
            state=self._script.get(task_id, "running"),
        )
        self._attempts[attempt.attempt_id] = attempt
        return attempt

    # ---------------------------------------------------------------- 查询
    def query(self, attempt_id: str) -> AttemptState:
        return self._attempts[attempt_id].state

    def attempts_of(self, task_id: str) -> list[FakeAttempt]:
        return [a for a in self._attempts.values() if a.task_id == task_id]

    # ---------------------------------------------------------------- 控制
    def finish(self, attempt_id: str, state: AttemptState,
               artifacts: list[str] | None = None) -> FakeAttempt:
        attempt = self._attempts[attempt_id]
        attempt.state = state
        if artifacts is not None:
            attempt.artifacts = list(artifacts)
        return attempt

    def cancel(self, attempt_id: str, *, provider_confirms: bool = True) -> FakeAttempt:
        """取消。`provider_confirms=False` 模拟"远端没有确认"——此时状态不明。"""
        attempt = self._attempts[attempt_id]
        attempt.cancel_requested = True
        if provider_confirms:
            attempt.cancel_confirmed = True
            attempt.state = "cancelled"
        else:
            attempt.state = "outcome_unknown"
        return attempt


def fake_executor_for(plan, script: dict[str, AttemptState] | None = None) -> FakeExecutor:
    """按计划里的任务编号建一个替身，便于测试直接 `executor.query(...)`。"""
    del plan  # 只为可读性保留参数
    return FakeExecutor(script)

"""任务状态迁移表（issue #5 要求的 `orchestrator/state_machine/`）。

为什么单独一个模块：契约里 `TaskState` 只是八个字符串的 Literal，pydantic 拦不住
`accepted → running` 这种语义错误。合法边必须由状态机自己守，否则"取消与验收竞争"
这种场景只能靠测试兜，而不是靠代码结构。

三条不变量：
1. `accepted` 与 `cancelled` 是终态——不能从它们迁到任何状态。
2. 进入 `accepted` 只能来自 `verifying`（先有产物与复核）。
3. 回到 `ready` 只能来自 `blocked` / `failed`（重试），不能从 `running` 直接回退，
   否则一次在途执行会被另一次覆盖。
"""

from __future__ import annotations

ALLOWED: dict[str, frozenset[str]] = {
    "waiting": frozenset({"ready", "blocked", "cancelled"}),
    # ready → waiting 只用于改版回退：前驱因新版本失效时，任务退回等待
    "ready": frozenset({"running", "blocked", "cancelled", "waiting"}),
    "running": frozenset({"verifying", "failed", "blocked", "cancelled"}),
    "verifying": frozenset({"accepted", "failed"}),
    "accepted": frozenset(),
    "blocked": frozenset({"ready", "failed", "cancelled", "waiting"}),
    "failed": frozenset({"ready", "cancelled"}),
    "cancelled": frozenset(),
}

TERMINAL = frozenset({"accepted", "cancelled"})


class IllegalTransition(ValueError):
    """非法迁移。带上起止状态，便于事件与日志直接引用。"""

    def __init__(self, task_id: str, old: str, new: str) -> None:
        super().__init__(
            f"{task_id}: 非法迁移 {old} → {new}（允许 {sorted(ALLOWED.get(old, frozenset()))}）")
        self.task_id = task_id
        self.old = old
        self.new = new


def can_transition(old: str, new: str) -> bool:
    if old == new:
        return True
    return new in ALLOWED.get(old, frozenset())


def assert_transition(task_id: str, old: str, new: str) -> None:
    if not can_transition(old, new):
        raise IllegalTransition(task_id, old, new)

"""局部异常登记簿（issue #5："局部异常登记后继续独立支线"）。

为什么需要它，而不是直接把任务标成 `blocked` 就完事：

- `blocked` 只说"这个任务现在不能动"，没说明**为什么**、**谁该处理**、**处理完了没**。
- 调度器需要区分两类情况：一类可以自己重试（技术失败），一类必须等人（范围越界、
  结果未知、证据缺失）。后者要进"待人工"队列，否则界面只能显示一片灰。

登记的语义：**只记录，不改任务状态**。状态迁移仍由调度器/命令单独决定，
这样"登记了但还没处置"和"已经处置"在事件流里是两件事，不会混。

不变量：一条异常登记不会阻止**其他支线**继续派发。它是记账，不是刹车。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

IncidentKind = Literal[
    "execution_failed",     # 执行器报错
    "outcome_unknown",      # 已发送但结果不明，保留资源
    "scope_violation",      # 产物越出 write_scope
    "artifact_missing",     # 没有产物就宣称完成
    "stale_receipt",        # 迟到回执（代际过期）
]


@dataclass(frozen=True)
class Incident:
    task_id: str
    kind: IncidentKind
    reason: str
    needs_human: bool = True
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "reason": self.reason,
            "needs_human": self.needs_human,
            "at": self.at,
        }


class IncidentLedger:
    """按任务记录异常；同一任务可以有多条（例如先失败、后补证据仍不足）。"""

    def __init__(self) -> None:
        self._items: list[Incident] = []
        self._resolved: dict[str, str] = {}

    def add(self, task_id: str, kind: IncidentKind, reason: str,
            *, needs_human: bool = True) -> Incident:
        incident = Incident(task_id=task_id, kind=kind,
                            reason=reason[:160] or kind, needs_human=needs_human)
        self._items.append(incident)
        return incident

    def resolve(self, task_id: str, note: str) -> None:
        self._resolved[task_id] = note[:160] or "已处置"

    # ---------------------------------------------------------------- 查询
    def all(self) -> list[Incident]:
        return list(self._items)

    def open(self) -> list[Incident]:
        """尚未处置、且需要人处理的条目——界面顶部置顶显示这一批。"""
        return [i for i in self._items if i.needs_human and i.task_id not in self._resolved]

    def open_for(self, task_id: str) -> list[Incident]:
        return [i for i in self._items
                if i.task_id == task_id and task_id not in self._resolved]

    def resolved(self) -> dict[str, str]:
        return dict(self._resolved)

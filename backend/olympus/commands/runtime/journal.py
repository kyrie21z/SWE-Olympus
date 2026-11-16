"""命令事件的持久化插槽。

现状：`NullJournal`——**明确地不写**，并通过 HTTP 响应头把这件事告诉调用方。

为什么不直接写数据库？

`Store.commit` 一次只写一条事件，而 `start` 之类的命令经 `scheduler.reconcile()`
会产生多条（run 状态 + 每个被提升为 ready 的任务）。用现有接口落库只能二选一：
丢掉中间事件，或者造出与事件日志不一致的快照——两者都违反"事件不可变、可回放"。

因此这里先把边界留出来。等 P1 确认批量写入接口后，新增 `StoreJournal`
实现同一个 `CommandJournal` 协议即可，`CommandService` 与 API 都不需要改。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from olympus.contracts.models import Command, Event


class CommandJournal(Protocol):
    """一次已接纳命令所产生事件的记录者。"""

    durability: str
    name: str

    def record(self, events: Sequence[Event], command: Command) -> int:
        """记录事件，返回实际持久化的条数。"""


class NullJournal:
    """不落库的实现：返回 0，并由能力端点声明 durability=memory。"""

    durability = "memory"
    name = "null"

    def record(self, events: Sequence[Event], command: Command) -> int:
        return 0

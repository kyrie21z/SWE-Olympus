"""Explicit in-memory and atomic SQLite command journals."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from olympus.contracts.models import Command, Event, Receipt, Run
from olympus.storage.sqlite import Store


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


class StoreJournal:
    """Commit an entire command and its runtime metadata in a single transaction."""

    durability = "sqlite"
    name = "sqlite"

    def __init__(self, store: Store):
        self.store = store

    def lookup(self, command: Command) -> Receipt | None:
        return self.store.command_receipt(command)

    def commit(
        self, events: Sequence[Event], command: Command, receipt: Receipt, *, expected: Run,
        runtime_state: dict,
    ) -> tuple[Receipt, int]:
        return self.store.commit_command(
            command, receipt, events, expected=expected, runtime_state=runtime_state,
        )

    def record(self, events: Sequence[Event], command: Command) -> int:
        """Compatibility entry for trusted callers that already accepted a command."""
        expected = self.store.snapshot(command.run_id)
        final = events[-1].snapshot if events else expected
        receipt = Receipt(
            operation_id=command.operation_id, run_id=command.run_id,
            status="accepted", reason_code="ok", state_revision=final.state_revision,
        )
        _, recorded = self.store.commit_command(command, receipt, events, expected=expected)
        return recorded

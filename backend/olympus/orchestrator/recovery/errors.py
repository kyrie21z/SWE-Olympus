"""恢复边界上的显式失败。

恢复只有两种结果：要么整条事件链可证明地对得上，要么停下来报告缺口。
不存在"差不多能跑就先跑"的中间态——那正是这个项目反复拒绝的做法。
"""

from __future__ import annotations

from typing import Any


class RecoveryError(RuntimeError):
    """恢复过程的可预期失败，携带稳定的机器可读 code。"""

    code = "RECOVERY_FAILED"


class RecoveryGap(RecoveryError):
    """事件链与快照对不上：缺号、序号越界、修订号跳变或末条快照不一致。"""

    code = "RECOVERY_GAP"

    def __init__(self, run_id: str, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(f"{run_id}: {message}")
        self.run_id = run_id
        self.message = message
        self.detail = detail or {}

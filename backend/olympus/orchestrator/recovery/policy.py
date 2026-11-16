"""有界重试策略（Zeus 侧的"冻结策略"）。

规则直接抄自 PDF 第 5.2 节与 issue #9：

1. 本任务失败 → 带失败信息重试，**首次 + 最多两次自动重试**；
2. 归因为上游 → 不重试本任务，创建上游修复子任务；
3. 结果未知 → **先查询核对**，查询前禁止重发（P2 账本的硬规则）；
4. 额度用尽、归因不明、设计/测试问题 → 转人工。

两条容易被写错的边界：

- 策略**从不修改 `Run.calls_used`**。重试是否花钱由 P2 账本记录，
  "人工重试另记但不重置总预算"也由账本保证，这里只数自动重试次数；
- 自动额度按**责任任务**计数。归因为上游时，计数落在上游修复任务上，
  不会把本任务的额度偷走。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from olympus.agents.momus.failure import FailureAttribution

Action = Literal["query", "retry", "repair", "human"]

DEFAULT_MAX_AUTO_RETRIES = 2


@dataclass(frozen=True)
class RetryDecision:
    action: Action
    auto_retry_no: int
    reason: str


class BoundedRetryPolicy:
    def __init__(self, max_auto_retries: int = DEFAULT_MAX_AUTO_RETRIES) -> None:
        if max_auto_retries < 0:
            raise ValueError("max_auto_retries 不能为负")
        self.max_auto_retries = max_auto_retries
        self._auto: dict[str, int] = {}
        self._queried: set[str] = set()

    # ---------------------------------------------------------------- 未知核对
    def note_query(self, task_id: str) -> None:
        """记录"已经向提供方查询过"。查询本身不消耗模型调用额度。"""
        self._queried.add(task_id)

    # ---------------------------------------------------------------- 决策
    def decide(self, attribution: FailureAttribution, *, outcome_unknown: bool = False) -> RetryDecision:
        task_id = attribution.responsible_task_id
        used = self._auto.get(task_id, 0)

        if outcome_unknown:
            if task_id not in self._queried:
                return RetryDecision(
                    action="query",
                    auto_retry_no=used,
                    reason="结果未知：先查询提供方核对，禁止盲目重发",
                )
            return RetryDecision(
                action="human",
                auto_retry_no=used,
                reason="查询后仍无法确认结果，交人工核对（预算保持占用）",
            )

        if attribution.category == "upstream":
            return RetryDecision(
                action="repair",
                auto_retry_no=used,
                reason=f"归因为上游 {task_id}：创建修复子任务，本任务等待",
            )

        if attribution.category in ("design", "test"):
            return RetryDecision(
                action="human",
                auto_retry_no=used,
                reason=f"归因为{('设计' if attribution.category == 'design' else '测试')}问题：需上游改版，不自动重试",
            )

        if attribution.category == "unknown":
            return RetryDecision(
                action="human",
                auto_retry_no=used,
                reason="归因不明：不猜、不自动重试，交人工处理",
            )

        # 本任务失败
        if used >= self.max_auto_retries:
            return RetryDecision(
                action="human",
                auto_retry_no=used,
                reason=f"自动重试额度用尽（{self.max_auto_retries} 次），转人工",
            )
        self._auto[task_id] = used + 1
        return RetryDecision(
            action="retry",
            auto_retry_no=used + 1,
            reason=f"本任务失败：第 {used + 1}/{self.max_auto_retries} 次自动重试",
        )

    # ---------------------------------------------------------------- 观测
    def auto_retries(self, task_id: str) -> int:
        return self._auto.get(task_id, 0)

    def snapshot(self) -> dict[str, int]:
        return dict(self._auto)

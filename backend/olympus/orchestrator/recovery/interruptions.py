"""重启后的中断核对（issue #9 的"四处中断"）。

四个中断点，四种正确处置：

| 中断点 | 判据 | 处置 |
| --- | --- | --- |
| 发送前 | 账本 `prepared` | 本地取消，**不消耗调用额度** |
| 发送后未回 | 账本 `sent` / `outcome_unknown` | 有查询能力就查；查不到就保留未知、预算继续占用 |
| 工件已保存未接纳 | 任务 `verifying` | 保持未验证，等待独立验收；**不自动接受** |
| 已接纳 | 任务 `accepted` 且验收 `passed` | 保持已验收，**不重跑、不复核** |

一条硬规则贯穿全部：**核对不等于重发。** 本模块从不调用 `prepare(is_resend=True)`，
也不会把"查不到"写成"失败"。查不到就是查不到，预算继续冻着，等人工对账。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from olympus.orchestrator.state_machine import RunContext
from olympus.requests.ledger import LedgerEntry, RequestLedger

Point = Literal["before_send", "sent_unanswered", "artifact_saved", "accepted"]
POINTS: tuple[Point, ...] = ("before_send", "sent_unanswered", "artifact_saved", "accepted")


@dataclass(frozen=True)
class Interruption:
    point: Point
    subject_id: str
    action: str
    detail: str
    budget_held: bool = False
    needs_reconciliation: bool = False


@dataclass(frozen=True)
class ReconciliationReport:
    run_id: str
    interruptions: tuple[Interruption, ...]

    def by_point(self) -> dict[Point, list[Interruption]]:
        """四个点位都要出现，哪怕为空——没查过和查了没事是两回事。"""
        grouped: dict[Point, list[Interruption]] = {point: [] for point in POINTS}
        for item in self.interruptions:
            grouped[item.point].append(item)
        return grouped

    def summary(self) -> dict[str, int]:
        return {point: len(items) for point, items in self.by_point().items()}

    def needs_human(self) -> list[Interruption]:
        return [item for item in self.interruptions if item.needs_reconciliation]


class RestartReconciler:
    """把账本与运行态合起来，给出每个中断点的核对结论。"""

    def __init__(
        self,
        ledger: RequestLedger,
        provider_status_hook: Callable[[LedgerEntry], str | None] | None = None,
    ) -> None:
        self._ledger = ledger
        self._hook = provider_status_hook

    def reconcile(self, run_id: str, ctx: RunContext | None = None) -> ReconciliationReport:
        found: list[Interruption] = []

        for entry in self._ledger.entries(run_id):
            if entry.status == "prepared":
                result = self._ledger.cancel(entry.request_id)
                found.append(
                    Interruption(
                        point="before_send",
                        subject_id=entry.request_id,
                        action="cancelled_before_send",
                        detail="进程在发送前中断：本地取消，未消耗调用额度",
                        needs_reconciliation=result.needs_reconciliation,
                    )
                )
                continue

            if entry.status not in ("sent", "outcome_unknown"):
                continue

            if self._hook is None:
                # 没有查询能力时，最诚实的结果是"未知"，而不是猜成功或失败。
                self._ledger.record_outcome(entry.request_id, "outcome_unknown")
                found.append(
                    Interruption(
                        point="sent_unanswered",
                        subject_id=entry.request_id,
                        action="unverifiable_kept_unknown",
                        detail="已发送但无提供方查询能力：保留未知，预算继续占用",
                        budget_held=True,
                        needs_reconciliation=True,
                    )
                )
                continue

            queried = self._ledger.query(entry.request_id, provider_status_hook=self._hook)
            found.append(
                Interruption(
                    point="sent_unanswered",
                    subject_id=entry.request_id,
                    action="queried_terminal" if queried.is_terminal else "queried_unknown",
                    detail="已向提供方查询并核对",
                    budget_held=queried.budget_held,
                    needs_reconciliation=queried.needs_reconciliation,
                )
            )

        if ctx is not None:
            found.extend(_task_interruptions(ctx))

        return ReconciliationReport(run_id=run_id, interruptions=tuple(found))


def _task_interruptions(ctx: RunContext) -> list[Interruption]:
    out: list[Interruption] = []
    for task in ctx.run.tasks:
        if task.state == "verifying":
            out.append(
                Interruption(
                    point="artifact_saved",
                    subject_id=task.task_id,
                    action="awaiting_acceptance",
                    detail="产物已保存但尚未验收：保持未验证，等待独立复核",
                )
            )
        elif task.state == "accepted" and task.verification_status == "passed":
            out.append(
                Interruption(
                    point="accepted",
                    subject_id=task.task_id,
                    action="kept",
                    detail="已验收：不重跑、不复核，迟到响应不得覆盖",
                )
            )
    return out

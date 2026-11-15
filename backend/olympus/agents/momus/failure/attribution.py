"""失败归因（Momus 职责的离线版本）。

设计原则和项目其他地方一致：

- **能证明的才说**。没有模型参与时，`mode` 固定为 `heuristic`，
  不冒充在线语义复核；
- **归因要带证据**。每条判断都附带触发它的具体事实，方便人工复核；
- **归因不是处置**。这里只说"责任落在哪一层"，是否重试、是否建修复任务，
  由 Zeus 侧的冻结策略决定（见 `orchestrator/recovery/policy.py`）。

PDF 第 5.2 节的四种归因：本任务、上游、设计、测试有误。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Category = Literal["task", "upstream", "design", "test", "unknown"]


@dataclass(frozen=True)
class FailureEvidence:
    """一次失败的可核对事实，全部来自执行记录与调度器状态。"""

    task_id: str
    outcome: Literal["failed", "outcome_unknown"] = "failed"
    missing_inputs: tuple[str, ...] = ()
    upstream_candidates: tuple[str, ...] = ()
    scope_violations: tuple[str, ...] = ()
    check_failures: tuple[str, ...] = ()
    design_mismatches: tuple[str, ...] = ()


@dataclass(frozen=True)
class FailureAttribution:
    category: Category
    responsible_task_id: str
    reasons: tuple[str, ...]
    mode: Literal["heuristic", "model"] = "heuristic"


class HeuristicFailureAttributor:
    """按固定优先级归类，规则少而明确，宁可判 unknown 也不猜。"""

    def attribute(self, evidence: FailureEvidence) -> FailureAttribution:
        if evidence.outcome == "outcome_unknown":
            return FailureAttribution(
                category="unknown",
                responsible_task_id=evidence.task_id,
                reasons=("调用结果未知，必须先向提供方查询核对",),
            )

        if evidence.design_mismatches:
            return FailureAttribution(
                category="design",
                responsible_task_id=evidence.task_id,
                reasons=evidence.design_mismatches,
            )

        if evidence.scope_violations:
            return FailureAttribution(
                category="task",
                responsible_task_id=evidence.task_id,
                reasons=tuple(f"越界写入：{item}" for item in evidence.scope_violations),
            )

        # 缺的是上游产物，且能指出具体来源 → 责任在上游，不在本任务。
        if evidence.missing_inputs and evidence.upstream_candidates:
            return FailureAttribution(
                category="upstream",
                responsible_task_id=evidence.upstream_candidates[0],
                reasons=tuple(f"缺少上游产物：{item}" for item in evidence.missing_inputs),
            )

        if evidence.check_failures:
            return FailureAttribution(
                category="task",
                responsible_task_id=evidence.task_id,
                reasons=evidence.check_failures,
            )

        return FailureAttribution(
            category="unknown",
            responsible_task_id=evidence.task_id,
            reasons=("没有足够证据判断责任层，交人工处理",),
        )

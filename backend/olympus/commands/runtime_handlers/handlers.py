"""运行期人工命令处理器（issue #9 第 4 条工作包）。

四个动作全部只走 P3 已有的合法状态边，**不新增迁移、不改核心核**：

| 动作 | 状态变化 | 为什么 |
| --- | --- | --- |
| `retry` | `failed`/`blocked` → `ready` | 合法边只允许从这两种状态回到 ready |
| `accept_risk` | `blocked` → `cancelled` | 终态且不是 `accepted`，因此不解锁验证型后继 |
| `reassign` | 不变 | 执行者覆盖由本层登记，派发时读取；计划本身不可变 |
| `request_change` | 不变 | 变更由上游处理，本层只登记并标注回退层级 |

复制自 issue 的两条硬约束：

1. 人工重试**另记但不重置总预算**——本模块从不触碰 `run.calls_used`；
2. 风险接受**不增加 accepted、不解锁验证型后继**——落到 `cancelled`，
   且 `verification_status` 保持 `unverified`，矩阵显示"未验证 · 人工接受"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from olympus.commands.core.bus import CommandBus
from olympus.commands.registry.actions import ACTION_PRECONDITION
from olympus.contracts.models import Command, Run, TaskRuntime
from olympus.orchestrator.scheduler import Scheduler
from olympus.orchestrator.state_machine import RunContext

CHANGE_LEVELS = ("task", "design", "requirement")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _require_precondition(ctx: RunContext, action: str) -> None:
    """用注册表里的前置 run 状态做校验，避免界面和处理器各写一套判据。"""
    allowed = ACTION_PRECONDITION.get(action, frozenset())
    if ctx.run.status not in allowed:
        raise ValueError(f"run 当前为 {ctx.run.status}，{action} 需要 {'/'.join(sorted(allowed))}")


def _task_of(run: Run, task_id: str) -> TaskRuntime:
    task = next((item for item in run.tasks if item.task_id == task_id), None)
    if task is None:
        raise KeyError(f"未知任务 {task_id}")
    return task


@dataclass
class RuntimeCommandHandlers:
    """四个处理器 + 一份进程内的命令痕迹。

    痕迹（提示原文、理由、改派目标）不是权威状态，而是给人看的审计线索；
    权威状态仍在 `Run` 快照与事件里。落库能力到位后，可把痕迹一并传入 journal。
    """

    _assignments: dict[str, dict[str, str]] = field(default_factory=dict)
    _trail: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    # ---------------------------------------------------------------- 注册
    def register(self, bus: CommandBus) -> None:
        bus.register(
            "retry",
            self.retry,
            pre="任务处于 failed / blocked",
            effect="回到 ready，generation+1，预算不重置",
        )
        bus.register(
            "reassign",
            self.reassign,
            pre="任务处于 ready / blocked",
            effect="登记人工执行者覆盖，任务状态不变",
        )
        bus.register(
            "accept_risk",
            self.accept_risk,
            pre="任务处于 blocked，且必须填写理由",
            effect="转 cancelled 并标记人工接受，不解锁后继",
        )
        bus.register(
            "request_change",
            self.request_change,
            pre="run 处于 active / paused",
            effect="登记变更请求并标注回退层级，本层不擅自改计划",
        )

    # ---------------------------------------------------------------- 处理器
    def retry(self, cmd: Command, ctx: RunContext, sched: Scheduler) -> tuple[str, str, str]:
        _require_precondition(ctx, "retry")
        task_id = cmd.target_id
        runtime = _task_of(ctx.run, task_id)
        if runtime.state not in ("failed", "blocked"):
            raise ValueError(f"{task_id} 当前为 {runtime.state}，只有 failed/blocked 可重试")

        prompt = _text(cmd.parameters.get("prompt"))

        def mutate(snapshot: Run) -> None:
            task = _task_of(snapshot, task_id)
            # 先复位验证态再改状态：TaskRuntime 的赋值校验要求两者自洽。
            task.verification_status = "unverified"
            task.generation += 1
            task.state = "ready"

        reason = f"人工重试 {task_id}" + (f"：{prompt}" if prompt else "")
        ctx.commit(
            reason=reason,
            actor=cmd.actor,
            actor_type="human",
            mutate=mutate,
            task_id=task_id,
        )
        entry = self._record(ctx, cmd, "retry", task_id, prompt=prompt)
        entry["generation"] = _task_of(ctx.run, task_id).generation
        entry["human_retry_count"] = self._retry_count(ctx.run.run_id, task_id)
        return (
            "accepted",
            "ok",
            f"{task_id} 已回到 ready（第 {entry['human_retry_count']} 次人工重试），总预算未重置",
        )

    def accept_risk(self, cmd: Command, ctx: RunContext, sched: Scheduler) -> tuple[str, str, str]:
        _require_precondition(ctx, "accept_risk")
        task_id = cmd.target_id
        runtime = _task_of(ctx.run, task_id)
        if runtime.state != "blocked":
            raise ValueError(f"{task_id} 当前为 {runtime.state}，只有 blocked 可接受风险")
        if runtime.verification_status == "passed":
            raise ValueError("已通过验证的任务不需要接受风险")

        reason_text = _text(cmd.parameters.get("reason"))
        if not reason_text:
            raise ValueError("接受风险必须填写理由")

        def mutate(snapshot: Run) -> None:
            task = _task_of(snapshot, task_id)
            # 赋值顺序不能换：TaskRuntime 开了 validate_assignment，
            # 每赋一个值就立即校验一次。必须先把理由写进去，
            # 否则 disposition 变成 risk_accepted 的瞬间会因为"没有理由"而失败。
            task.disposition_reason = reason_text
            task.state = "cancelled"
            task.disposition = "risk_accepted"

        ctx.commit(
            reason=f"接受风险 {task_id}：{reason_text}",
            actor=cmd.actor,
            actor_type="human",
            mutate=mutate,
            task_id=task_id,
        )
        self._record(ctx, cmd, "accept_risk", task_id, reason=reason_text)
        return (
            "accepted",
            "ok",
            f"{task_id} 已标记未验证·人工接受；不计入 accepted，也不解锁验证型后继",
        )

    def reassign(self, cmd: Command, ctx: RunContext, sched: Scheduler) -> tuple[str, str, str]:
        _require_precondition(ctx, "reassign")
        task_id = cmd.target_id
        runtime = _task_of(ctx.run, task_id)
        if runtime.state not in ("ready", "blocked"):
            raise ValueError(f"{task_id} 当前为 {runtime.state}，只有 ready/blocked 可改派")

        assignee = _text(cmd.parameters.get("assignee"))
        if not assignee:
            raise ValueError("改派必须指定执行者")

        ctx.commit(
            reason=f"改派 {task_id} → {assignee}",
            actor=cmd.actor,
            actor_type="human",
            mutate=lambda snapshot: None,
            task_id=task_id,
        )
        self._assignments.setdefault(ctx.run.run_id, {})[task_id] = assignee
        self._record(ctx, cmd, "reassign", task_id, assignee=assignee)
        return (
            "accepted",
            "ok",
            f"{task_id} 已登记改派给 {assignee}；产物仍走同一套独立验收",
        )

    def request_change(self, cmd: Command, ctx: RunContext, sched: Scheduler) -> tuple[str, str, str]:
        _require_precondition(ctx, "request_change")
        level = _text(cmd.parameters.get("level")).lower()
        if level not in CHANGE_LEVELS:
            raise ValueError(f"level 必须是 {' / '.join(CHANGE_LEVELS)} 之一")
        summary = _text(cmd.parameters.get("summary"))
        if not summary:
            raise ValueError("变更请求必须说明内容")

        ctx.commit(
            reason=f"变更请求[{level}] {summary}",
            actor=cmd.actor,
            actor_type="human",
            mutate=lambda snapshot: None,
        )
        self._record(ctx, cmd, "request_change", cmd.target_id, level=level, summary=summary)
        where = {
            "task": "回到任务规划，重新生成计划版本",
            "design": "回到设计规格，更新 DS 后重新确认",
            "requirement": "退回上游需求阶段；当前运行继续",
        }[level]
        return "accepted", "recorded_upstream", f"变更已登记：{where}"

    # ---------------------------------------------------------------- 痕迹
    def trail(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self._trail.get(run_id, [])]

    def assignments(self, run_id: str) -> dict[str, str]:
        return dict(self._assignments.get(run_id, {}))

    def _retry_count(self, run_id: str, task_id: str) -> int:
        return sum(
            1
            for entry in self._trail.get(run_id, [])
            if entry["action"] == "retry" and entry["task_id"] == task_id
        )

    def _record(self, ctx: RunContext, cmd: Command, action: str, task_id: str, **extra: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "action": action,
            "task_id": task_id,
            "actor": cmd.actor,
            "operation_id": cmd.operation_id,
            "at": _now(),
            "state_revision": ctx.run.state_revision,
            **extra,
        }
        self._trail.setdefault(ctx.run.run_id, []).append(entry)
        return entry

"""人工命令核（P3 · OL-06）。

与团队既有产物的关系（这是本模块第二版的设计依据）：

| 职责 | 谁提供 | 本模块怎么做 |
| --- | --- | --- |
| CAS、幂等键、事件与快照持久化 | P1 `storage.sqlite.Store.commit()` | 优先委托；无 store 时用内存实现（测试与无库场景） |
| 动作 → 允许角色、前置状态、待移交清单 | `commands.registry.actions` | 只读消费 |
| 鉴权、内容版本、目标版本、前置状态、`allowed_actions` | P3 本模块 | 命令核 |

issue #6 的三条硬要求：

1. **鉴权/版本/幂等/CAS、处理器注册、`allowed_actions` 与拒绝原因查询**
   → `dispatch()` 六道闸门 + `allowed_actions()` 逐动作给出可用性与原因。
2. **只落地设计/计划批准与驳回、运行许可、停止新派发/受控恢复、最终确认**
   → `CORE_ACTIONS` 才有处理器。
3. **重试/改派/风险接受/变更修复移交 P5，本段用假处理器验证注册，
   未注册动作返回 `NOT_READY`，不假装实现**
   → `DEFERRED_ACTIONS` 一律 `NOT_READY`；P5 通过 `register()` 接管，不改本文件。

`Receipt` 没有 detail 字段（契约如此），所以拒绝原因放 `notes[operation_id]` 与事件里。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from olympus.commands.registry.actions import (
    ACTION_ACTORS,
    ACTION_PRECONDITION,
    DEFERRED_ACTIONS,
    is_deferred,
)
from olympus.contracts.models import Command, Receipt, Run, TaskRuntime
from olympus.orchestrator.scheduler.scheduler import Scheduler
from olympus.orchestrator.state_machine.run_context import RunContext

Handler = Callable[[Command, RunContext, Scheduler], tuple[str, str, str]]
# 返回 (status, reason_code, note)


@dataclass
class _Spec:
    action: str
    fn: Handler
    actors: frozenset[str]
    pre: str
    effect: str


@dataclass
class CommandBus:
    ctx: RunContext
    scheduler: Scheduler
    store: Any = None            # 可选：olympus.storage.sqlite.Store，给了就委托持久化
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _receipts: dict[str, tuple[str, Receipt]] = field(default_factory=dict, repr=False)
    notes: dict[str, str] = field(default_factory=dict, repr=False)
    _specs: dict[str, _Spec] = field(default_factory=dict, repr=False)
    _deferred: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._register_builtin()

    # ------------------------------------------------------------ 注册
    def register(self, action: str, fn: Handler, actors: set[str] | None = None,
                 pre: str = "", effect: str = "") -> None:
        """注册一个处理器。P5 用这个方法接管 DEFERRED_ACTIONS，不需要改本文件。"""
        self._specs[action] = _Spec(
            action=action,
            fn=fn,
            actors=frozenset(actors or ACTION_ACTORS.get(action, {"YOU"})),
            pre=pre or _pre_text(action),
            effect=effect or "已注册处理器",
        )
        self._deferred.pop(action, None)

    def register_deferred(self, action: str, reason: str) -> None:
        """登记一个"本段不实现"的动作，dispatch 时统一返回 NOT_READY。"""
        self._deferred[action] = reason

    def catalog(self) -> list[dict]:
        """完整动作清单（含未注册的），供界面渲染"允许操作 + 禁用原因"。"""
        rows = []
        for action in sorted(set(ACTION_ACTORS) | set(self._specs)):
            if action in self._specs:
                spec = self._specs[action]
                rows.append({"action": action, "actors": sorted(spec.actors),
                             "pre": spec.pre, "effect": spec.effect,
                             "implemented": True})
            else:
                rows.append({"action": action,
                             "actors": sorted(ACTION_ACTORS.get(action, set())),
                             "pre": _pre_text(action),
                             "effect": self._deferred.get(action, "未实现"),
                             "implemented": False})
        return rows

    # ------------------------------------------------------------ 允许动作
    def allowed_actions(self, actor: str = "YOU") -> list[dict]:
        """当前状态下每个动作能不能发、为什么不能。

        界面「按钮禁用原因」直接读这个；P4/P5/P6 不需要各自再实现一遍判据。
        """
        run_status = self.ctx.run.status
        out: list[dict] = []
        for row in self.catalog():
            action = row["action"]
            allowed_pre = ACTION_PRECONDITION.get(action, frozenset())
            reasons: list[str] = []

            if not row["implemented"]:
                reasons.append(self._deferred.get(action, "本段未实现，待 P5 接管"))
            if actor not in row["actors"]:
                reasons.append(f"{actor} 无权执行（允许 {row['actors']}）")
            if allowed_pre and run_status not in allowed_pre:
                reasons.append(f"需要 run 处于 {sorted(allowed_pre)}，当前 {run_status}")
            reasons.extend(self._task_level_reasons(action))

            out.append({"action": action, "enabled": not reasons,
                        "reasons": reasons, "actors": row["actors"]})
        return out

    def _task_level_reasons(self, action: str) -> list[str]:
        """任务级动作的前置条件：没有可操作任务时说明原因。"""
        states = {
            "retry": ("failed", "blocked"),
            "reassign": ("waiting", "ready", "blocked"),
            "accept_risk": ("failed", "blocked"),
        }.get(action)
        if states is None:
            return []
        candidates = [t.task_id for t in self.scheduler.plan.tasks
                      if self.ctx.state_of(t.task_id) in states]
        if not candidates:
            return [f"没有处于可 {action} 状态的任务"]
        return []

    # ------------------------------------------------------------ 提交
    def dispatch(self, cmd: Command) -> Receipt:
        digest = _digest(cmd)
        with self._lock:
            if cmd.run_id != self.ctx.run.run_id:
                return self._seal(cmd, digest, "rejected", "unknown_run",
                                  f"run {cmd.run_id} 不存在")

            cached = self._receipts.get(cmd.operation_id)
            if cached is not None:
                old_digest, old_receipt = cached
                if old_digest == digest:
                    return old_receipt
                return self._seal(cmd, digest, "rejected", "conflict",
                                  "同一 operation_id 载荷不同")

            if cmd.expected_state_revision != self.ctx.run.state_revision:
                return self._seal(
                    cmd, digest, "rejected", "stale",
                    f"期望 rev{cmd.expected_state_revision}，"
                    f"当前 rev{self.ctx.run.state_revision}")

            version_note = self._version_mismatch(cmd)
            if version_note:
                return self._seal(cmd, digest, "rejected", "stale_version", version_note)

            if is_deferred(cmd.action) and cmd.action not in self._specs:
                return self._seal(
                    cmd, digest, "rejected", "not_ready",
                    self._deferred.get(cmd.action, f"{cmd.action} 未实现，待 P5 注册处理器"))

            spec = self._specs.get(cmd.action)
            if spec is None:
                return self._seal(cmd, digest, "rejected", "bad_args",
                                  f"未知命令 {cmd.action}")
            if cmd.actor not in spec.actors:
                return self._seal(cmd, digest, "rejected", "forbidden",
                                  f"{cmd.actor} 无权执行 {cmd.action}"
                                  f"（允许 {sorted(spec.actors)}）")

            try:
                status, reason_code, note = spec.fn(cmd, self.ctx, self.scheduler)
            except (ValueError, KeyError) as exc:
                return self._seal(cmd, digest, "rejected", "bad_state", str(exc))
            receipt = self._seal(cmd, digest, status, reason_code, note)
            self._persist(cmd, note)
            return receipt

    def _version_mismatch(self, cmd: Command) -> str | None:
        task = next((t for t in self.scheduler.plan.tasks if t.task_id == cmd.target_id), None)
        if task is not None:
            if cmd.expected_version != task.version:
                return f"任务版本 {task.version} 与期望 {cmd.expected_version} 不符"
            return None
        if cmd.expected_version != self.scheduler.plan.version:
            return f"计划版本 {self.scheduler.plan.version} 与期望 {cmd.expected_version} 不符"
        return None

    def _persist(self, cmd: Command, note: str) -> None:
        """有 Store 时把这次操作落库（幂等键与回执交给它管理）。

        存储层故障不该让内存态与回执失效：记在 notes 里，由 P5 对账。
        """
        if self.store is None:
            return
        try:
            self.store.commit(cmd, self.ctx.run,
                              reason=note[:160] or cmd.action, actor_type="human")
        except Exception:                      # noqa: BLE001
            self.notes[f"{cmd.operation_id}:store"] = "存储提交失败，待对账"

    def _seal(self, cmd: Command, digest: str, status: str, reason_code: str,
              note: str) -> Receipt:
        receipt = Receipt(
            operation_id=cmd.operation_id,
            run_id=cmd.run_id,
            status=status,                                   # type: ignore[arg-type]
            reason_code=reason_code[:160],
            state_revision=self.ctx.run.state_revision,
        )
        self._receipts[cmd.operation_id] = (digest, receipt)
        self.notes[cmd.operation_id] = note
        return receipt

    # ------------------------------------------------------------ 内置命令
    def _register_builtin(self) -> None:
        self.register("approve_design", self._approve_design, {"YOU"},
                      "run 处于 draft，且批准包内含设计", "记录设计确认事件，不启动执行")
        self.register("approve_plan", self._approve_plan, {"YOU"},
                      "run 处于 draft，计划已通过 P2 审查", "记录计划批准事件，仍不可派发")

        self.register("start", self._start, {"YOU"}, "run 处于 draft", "run → active，允许派发")
        self.register("pause", self._pause, {"YOU", "Zeus"}, "run 处于 active",
                      "run → paused，停新派发，在途保留")
        self.register("resume", self._resume, {"YOU"}, "run 处于 paused",
                      "run → active，预算不清零")
        self.register("confirm_delivery", self._confirm_delivery, {"YOU"},
                      "全部必需任务 accepted", "run → completed")

        # 按 issue #6：这四类移交 P5 #9，本段只登记 + NOT_READY
        for action in DEFERRED_ACTIONS:
            self.register_deferred(action, f"{action} 移交 P5 #9，本段不实现")

    # ----- 设计与计划
    def _approve_design(self, cmd: Command, ctx: RunContext, sched: Scheduler):
        _require_status(ctx, {"draft"}, "approve_design")
        ctx.commit(reason=f"设计确认：{cmd.target_id}", actor=cmd.actor, actor_type="human",
                   mutate=lambda snap: None)
        return "accepted", "ok", f"设计 {cmd.target_id} 已确认（不启动执行）"

    def _approve_plan(self, cmd: Command, ctx: RunContext, sched: Scheduler):
        _require_status(ctx, {"draft"}, "approve_plan")
        ctx.commit(reason=f"计划批准：{cmd.target_id}", actor=cmd.actor, actor_type="human",
                   mutate=lambda snap: None)
        return "accepted", "ok", "计划已批准；仍需 start 才允许派发"


    # ----- run 生命周期
    def _start(self, cmd: Command, ctx: RunContext, sched: Scheduler):
        _require_status(ctx, {"draft"}, "start")
        ctx.set_run_status("active", reason="人工启动运行", actor=cmd.actor)
        sched.reconcile()
        return "accepted", "ok", "run 已激活，允许派发"

    def _pause(self, cmd: Command, ctx: RunContext, sched: Scheduler):
        _require_status(ctx, {"active"}, "pause")
        ctx.set_run_status("paused", reason="人工暂停：停止新派发", actor=cmd.actor)
        return "accepted", "ok", f"已暂停；在途 {len(sched._permits)} 个许可保留"

    def _resume(self, cmd: Command, ctx: RunContext, sched: Scheduler):
        _require_status(ctx, {"paused"}, "resume")
        ctx.set_run_status("active", reason="人工恢复：预算不清零", actor=cmd.actor)
        return "accepted", "ok", "已恢复"

    def _confirm_delivery(self, cmd: Command, ctx: RunContext, sched: Scheduler):
        _require_status(ctx, {"active", "awaiting_confirmation"}, "confirm_delivery")
        pending = [t.task_id for t in sched.plan.tasks
                   if t.required and ctx.state_of(t.task_id) != "accepted"]
        if pending:
            raise ValueError(f"仍有必需任务未验收：{pending}")
        ctx.set_run_status("completed", reason="人工确认交付", actor=cmd.actor)
        return "accepted", "ok", "交付已确认"


# ---------------------------------------------------------------- helper
def _require_status(ctx: RunContext, allowed: set[str], action: str) -> None:
    if ctx.run.status not in allowed:
        raise ValueError(f"{action} 需要 run 处于 {sorted(allowed)}，当前 {ctx.run.status}")


def _pre_text(action: str) -> str:
    states = ACTION_PRECONDITION.get(action)
    return f"run 处于 {sorted(states)}" if states else "任意状态"


def _risk_mutate(snapshot: Run, task_id: str, reason: str) -> None:
    """（保留给 P5 参考）接受风险：先写理由再写处置，否则 TaskRuntime 校验失败。"""
    runtime: TaskRuntime = next(t for t in snapshot.tasks if t.task_id == task_id)
    runtime.disposition_reason = reason
    runtime.disposition = "risk_accepted"


def _digest(cmd: Command) -> str:
    """业务载荷指纹：只覆盖"这次请求到底要做什么"。

    `expected_state_revision` 刻意排除——它是并发守卫，不是载荷内容。
    """
    raw = json.dumps(
        {
            "run": cmd.run_id,
            "actor": cmd.actor,
            "action": cmd.action,
            "target": cmd.target_id,
            "version": cmd.expected_version,
            "params": cmd.parameters,
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

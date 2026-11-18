"""进程内运行态注册表：`run_id` → 运行态。

三条边界：

1. 一个 run 在进程内只有**一份**运行态，重复获取返回同一个对象；
2. 首次获取时从事件 + 快照重建，重建失败直接抛 `RecoveryGap`，**不缓存半成品**；
3. 计划只能绑定一次：重复绑定会覆盖许可与执行者占用，属于状态破坏。

绑定、调度许可和审计线索与 Run 事件批次一并保存；命令事务由 StoreJournal 提交。
"""

from __future__ import annotations

import threading
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass, field

from olympus.commands.core.bus import CommandBus
from olympus.commands.runtime_handlers import RuntimeCommandHandlers
from olympus.contracts.models import Design, Plan, Ref, Requirement, Run
from olympus.orchestrator.scheduler import (
    DEFAULT_EXECUTOR_CAPACITY,
    DEFAULT_MAX_PARALLEL,
    Scheduler,
)
from olympus.orchestrator.state_machine import RunContext
from olympus.orchestrator.scheduler.incidents import Incident
from olympus.orchestrator.scheduler.scheduler import Permit
from olympus.planning import CompiledManifest
from olympus.planning.compiler.compiler import verify_manifest_binding
from olympus.storage.sqlite import Store
from olympus.traceability.planning.indexer import (
    PlanningTraceabilityIndex,
    build_planning_traceability,
)

from .rebuild import rebuild_context
from .errors import RecoveryGap


@dataclass
class RunRuntime:
    """一个运行在内存里的完整形态。"""

    run_id: str
    ctx: RunContext
    plan: Plan | None = None
    scheduler: Scheduler | None = None
    bus: CommandBus | None = None
    handlers: RuntimeCommandHandlers | None = None
    repairs: list[dict] = field(default_factory=list)
    requirement: Requirement | None = None
    design: Design | None = None
    trace: PlanningTraceabilityIndex | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _store: Store | None = field(default=None, repr=False)

    @property
    def has_plan(self) -> bool:
        return self.scheduler is not None

    def attach_plan(
        self,
        plan: Plan,
        manifest: CompiledManifest,
        *,
        requirement: Requirement | None = None,
        design: Design | None = None,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
        executor_capacity: int = DEFAULT_EXECUTOR_CAPACITY,
        persist: bool = True,
    ) -> RunRuntime:
        with self.lock:
            return self._attach_plan(
                plan, manifest, requirement=requirement, design=design,
                max_parallel=max_parallel, executor_capacity=executor_capacity, persist=persist,
            )

    def _attach_plan(
        self, plan: Plan, manifest: CompiledManifest, *, requirement: Requirement | None,
        design: Design | None, max_parallel: int, executor_capacity: int, persist: bool,
    ) -> RunRuntime:
        """绑定已批准的计划，建立调度器与命令总线。

        只能绑一次：第二次会把许可、执行者占用和尝试记录全部重置，
        而恢复出来的运行态无法证明这些内存记录可以安全丢弃。
        """
        if self.scheduler is not None:
            raise RuntimeError("该运行已绑定计划，重复绑定会重置许可与执行者占用")
        checkpoint = self.checkpoint()
        verify_manifest_binding(manifest, plan)
        self.plan = plan
        self.scheduler = Scheduler(
            plan=plan,
            manifest=manifest,
            ctx=self.ctx,
            max_parallel=max_parallel,
            executor_capacity=executor_capacity,
        )
        self.bus = CommandBus(ctx=self.ctx, scheduler=self.scheduler)
        # 四个运行期人工动作在这一刻从 NOT_READY 变成真实处理器（issue #9）。
        self.handlers = RuntimeCommandHandlers()
        self.handlers.register(self.bus)

        # 需求与设计同时提供时才有追踪索引；缺任一份就保持 None，
        # API 会明确返回"追踪不可用"，而不是拼一份假的。
        if requirement is not None and design is not None:
            self.requirement = requirement
            self.design = design
            self.trace = build_planning_traceability(requirement, design, plan)
        if persist and self._store is not None:
            try:
                self.persist()
            except Exception:
                self.restore_checkpoint(checkpoint)
                raise
        return self

    def export_state(self) -> dict:
        """Trusted host metadata needed to rebuild the same bound runtime."""
        state = {
            "run_id": self.run_id,
            "plan": self.plan.model_dump(mode="json") if self.plan else None,
            "requirement": self.requirement.model_dump(mode="json") if self.requirement else None,
            "design": self.design.model_dump(mode="json") if self.design else None,
            "repairs": deepcopy(self.repairs),
        }
        if self.scheduler is not None:
            sched = self.scheduler
            state["scheduler"] = {
                "manifest": sched.manifest.model_dump(mode="json"),
                "max_parallel": sched.max_parallel,
                "executor_capacity": sched.executor_capacity,
                "permits": {task_id: asdict(permit) for task_id, permit in sched._permits.items()},
                "executor_load": dict(sched._executor_load),
                "dispatch_count": dict(sched._dispatch_count),
                "attempts": dict(sched._attempts),
                "incidents": [item.to_dict() for item in sched._incidents.all()],
                "resolved": sched._incidents.resolved(),
            }
        if self.handlers is not None:
            state["assignments"] = self.handlers.assignments(self.run_id)
            state["trail"] = self.handlers.trail(self.run_id)
        if self.bus is not None:
            state["targets"] = [ref.model_dump(mode="json") for ref in self.bus._target_refs.values()]
            state["notes"] = dict(self.bus.notes)
        return state

    def restore_state(self, state: dict) -> None:
        """Restore bindings and reservations without dispatching work or reconciling."""
        if state.get("run_id") != self.run_id:
            raise ValueError("runtime metadata belongs to a different run")
        requirement = Requirement.model_validate(state["requirement"]) if state.get("requirement") else None
        design = Design.model_validate(state["design"]) if state.get("design") else None
        self.requirement, self.design = requirement, design
        if state.get("plan"):
            plan = Plan.model_validate(state["plan"])
            if {task.task_id for task in plan.tasks} != {task.task_id for task in self.ctx.run.tasks}:
                raise ValueError("saved plan and run tasks do not match")
            saved = state["scheduler"]
            manifest = CompiledManifest.model_validate(saved["manifest"])
            verify_manifest_binding(manifest, plan)
            if self.scheduler is None:
                self.attach_plan(
                    plan, manifest, requirement=requirement, design=design,
                    max_parallel=saved["max_parallel"], executor_capacity=saved["executor_capacity"],
                    persist=False,
                )
            else:
                self.plan = self.scheduler.plan = plan
                self.scheduler.manifest = manifest
                self.scheduler.max_parallel = saved["max_parallel"]
                self.scheduler.executor_capacity = saved["executor_capacity"]
                self.refresh_trace(plan)
            sched = self.scheduler
            sched._permits = {
                task_id: Permit(**(payload | {
                    "write_scope": tuple(payload["write_scope"]), "resources": tuple(payload["resources"]),
                })) for task_id, payload in saved.get("permits", {}).items()
            }
            sched._executor_load = dict(saved.get("executor_load", {}))
            sched._dispatch_count = dict(saved.get("dispatch_count", {}))
            sched._attempts = dict(saved.get("attempts", {}))
            sched._incidents._items = [Incident(**item) for item in saved.get("incidents", [])]
            sched._incidents._resolved = dict(saved.get("resolved", {}))
        else:
            self.plan = self.trace = None
            if self.scheduler is not None:
                self.scheduler = self.bus = self.handlers = None
        self.repairs = deepcopy(state.get("repairs", []))
        if self.handlers is not None:
            self.handlers._assignments = {self.run_id: deepcopy(state.get("assignments", {}))}
            self.handlers._trail = {self.run_id: deepcopy(state.get("trail", []))}
        if self.bus is not None:
            self.bus._target_refs = {
                ref.object_id: ref for ref in (Ref.model_validate(raw) for raw in state.get("targets", []))
            }
            self.bus.notes = dict(state.get("notes", {}))

    def checkpoint(self) -> dict:
        """Capture mutable state while the host holds this runtime's lock."""
        return {
            "run": self.ctx.run.model_copy(deep=True),
            "events": deepcopy(self.ctx.events), "seq": self.ctx._seq,
            "state": self.export_state(),
            "receipts": deepcopy(self.bus._receipts) if self.bus else {},
        }

    def restore_checkpoint(self, saved: dict) -> None:
        self.ctx.run = saved["run"]
        self.ctx.events[:] = saved["events"]
        self.ctx._seq = saved["seq"]
        self.restore_state(saved["state"])
        if self.bus is not None:
            self.bus._receipts = saved["receipts"]

    def persist(self, *, expected: Run | None = None) -> int:
        """Append new context events plus metadata using one SQLite transaction."""
        if self._store is None:
            raise RuntimeError("runtime is not attached to a store")
        with self.lock, self.bus._lock if self.bus is not None else nullcontext():
            with self.scheduler._lock if self.scheduler is not None else nullcontext():
                return self._persist(expected=expected)

    def _persist(self, *, expected: Run | None = None) -> int:
        """Caller holds runtime, command and scheduler locks in that order."""
        baseline = expected if expected is not None else self._store.snapshot(self.run_id)
        events = [event for event in self.ctx.events if event.event_seq > baseline.event_seq]
        final = events[-1].snapshot if events else baseline
        if final != self.ctx.run:
            raise ValueError("runtime snapshot contains unrecorded changes")
        return self._store.append_events(
            self.run_id, events, expected=baseline, runtime_state=self.export_state(),
        )

    def refresh_trace(self, plan: Plan) -> PlanningTraceabilityIndex | None:
        """计划改版（例如新增修复任务）后重建追踪索引。"""
        if self.requirement is None or self.design is None:
            self.trace = None
            return None
        self.trace = build_planning_traceability(self.requirement, self.design, plan)
        return self.trace


class RuntimeRegistry:
    """线程安全的运行态缓存；缓存未命中时从存储重建。"""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._runtimes: dict[str, RunRuntime] = {}
        self._lock = threading.RLock()

    @property
    def store(self) -> Store:
        return self._store

    def get(self, run_id: str) -> RunRuntime:
        """取运行态；不存在则重建。未知运行抛 `KeyError`（由上层映射为 404）。"""
        with self._lock:
            runtime = self._runtimes.get(run_id)
            if runtime is not None:
                return runtime

            # 重建失败会抛 RecoveryGap，此处不写入缓存：
            # 宁可每次请求都失败得一样，也不要留下一个"看起来能用"的半成品。
            ctx = rebuild_context(self._store, run_id)
            runtime = RunRuntime(run_id=run_id, ctx=ctx, _store=self._store)
            try:
                saved = self._store.load_runtime(run_id)
                if saved is not None:
                    runtime.restore_state(saved)
            except (ValueError, KeyError, TypeError) as exc:
                raise RecoveryGap(run_id, "持久化运行元数据不完整或与快照不一致") from exc
            self._runtimes[run_id] = runtime
            return runtime

    def peek(self, run_id: str) -> RunRuntime | None:
        """只查内存，不触发重建。用于健康检查与调试。"""
        with self._lock:
            return self._runtimes.get(run_id)

    def forget(self, run_id: str) -> bool:
        """丢弃内存运行态；下次获取会重新从存储重建。"""
        with self._lock:
            return self._runtimes.pop(run_id, None) is not None

    def run_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._runtimes)

    def __len__(self) -> int:
        with self._lock:
            return len(self._runtimes)

"""进程内运行态注册表：`run_id` → 运行态。

三条边界：

1. 一个 run 在进程内只有**一份**运行态，重复获取返回同一个对象；
2. 首次获取时从事件 + 快照重建，重建失败直接抛 `RecoveryGap`，**不缓存半成品**；
3. 计划只能绑定一次：重复绑定会覆盖许可与执行者占用，属于状态破坏。

本模块只解决"读得回来"。命令执行后的落库不在这里，见后续里程碑。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from olympus.commands.core.bus import CommandBus
from olympus.commands.runtime_handlers import RuntimeCommandHandlers
from olympus.contracts.models import Plan
from olympus.orchestrator.scheduler import (
    DEFAULT_EXECUTOR_CAPACITY,
    DEFAULT_MAX_PARALLEL,
    Scheduler,
)
from olympus.orchestrator.state_machine import RunContext
from olympus.planning import CompiledManifest
from olympus.storage.sqlite import Store

from .rebuild import rebuild_context


@dataclass
class RunRuntime:
    """一个运行在内存里的完整形态。"""

    run_id: str
    ctx: RunContext
    plan: Plan | None = None
    scheduler: Scheduler | None = None
    bus: CommandBus | None = None
    handlers: RuntimeCommandHandlers | None = None

    @property
    def has_plan(self) -> bool:
        return self.scheduler is not None

    def attach_plan(
        self,
        plan: Plan,
        manifest: CompiledManifest,
        *,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
        executor_capacity: int = DEFAULT_EXECUTOR_CAPACITY,
    ) -> RunRuntime:
        """绑定已批准的计划，建立调度器与命令总线。

        只能绑一次：第二次会把许可、执行者占用和尝试记录全部重置，
        而恢复出来的运行态无法证明这些内存记录可以安全丢弃。
        """
        if self.scheduler is not None:
            raise RuntimeError("该运行已绑定计划，重复绑定会重置许可与执行者占用")
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
        return self


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
            runtime = RunRuntime(run_id=run_id, ctx=ctx)
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

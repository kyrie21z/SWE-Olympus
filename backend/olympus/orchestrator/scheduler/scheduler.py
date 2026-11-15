"""Zeus 动态调度（P3 · OL-05）。

与团队契约的关系（这是本模块存在的意义）：

| 数据 | 来源 | 本模块不重复计算 |
| --- | --- | --- |
| 任务定义、依赖、写范围、资源、优先级 | `contracts.models.Task` / `Plan` | —— |
| 拓扑序、前驱表、层级、关键路径排名、静态冲突索引 | `planning.compiler.CompiledManifest` | 不再自己算依赖闭包，不自己算静态冲突 |
| 任务运行态（state / verification / disposition / generation） | `contracts.models.TaskRuntime` | 不另建状态表 |
| 状态版本与事件 | `RunContext`（`Run` + `Event`） | 不另发扁平事件 |

三条调度规则（契约附录 E）：

1. **三个集合**：`eligible`（计划允许）→ `ready`（前驱产物已验收）→ `dispatchable`（还能拿到资源、人员、许可）。
2. **排序**：按 `manifest.critical_path.priority_rank` 升序，再按 `task_id` 稳定排序。
   不用模型返回顺序，也不用到达顺序。
3. **原子占用**：检查与占位在同一把锁内完成。`executor` 满额、`write_scope` 重叠、
   `resources` 撞键，三者任一命中都不派。

与上一版（自造 Task/Command）的差别：任务卡字段、优先级排名、静态冲突全部来自团队既有产物，
P3 只补"按运行态做动态判定"这一层。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from olympus.contracts.models import Plan, Run, Task
from olympus.orchestrator.state_machine.run_context import RunContext
from olympus.planning.compiler.compiler import CompiledManifest

from .executor import FakeExecutor
from .incidents import Incident, IncidentKind, IncidentLedger

DEFAULT_MAX_PARALLEL = 3
DEFAULT_EXECUTOR_CAPACITY = 1


@dataclass(frozen=True)
class Permit:
    """一次派发许可。释放前，别人拿不到同一执行者、同一写范围、同一资源键。"""

    task_id: str
    generation: int
    executor: str
    write_scope: tuple[str, ...]
    resources: tuple[str, ...]


@dataclass
class Scheduler:
    plan: Plan
    manifest: CompiledManifest
    ctx: RunContext
    max_parallel: int = DEFAULT_MAX_PARALLEL
    executor_capacity: int = DEFAULT_EXECUTOR_CAPACITY
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _permits: dict[str, Permit] = field(default_factory=dict, repr=False)
    _executor_load: dict[str, int] = field(default_factory=dict, repr=False)
    _dispatch_count: dict[str, int] = field(default_factory=dict, repr=False)
    _attempts: dict[str, str] = field(default_factory=dict, repr=False)
    _incidents: IncidentLedger = field(default_factory=IncidentLedger, repr=False)

    # ------------------------------------------------------------ 基础视图
    @property
    def tasks(self) -> dict[str, Task]:
        return {t.task_id: t for t in self.plan.tasks}

    def rank(self, task_id: str) -> int:
        return self.manifest.critical_path.priority_rank.get(task_id, 10**6)

    def _ordered(self, ids: list[str]) -> list[str]:
        return sorted(ids, key=lambda tid: (self.rank(tid), tid))

    def _predecessors(self, task_id: str) -> list[str]:
        return list(self.manifest.predecessors.get(task_id, []))

    def _predecessors_accepted(self, task_id: str) -> bool:
        return all(self.ctx.state_of(p) == "accepted" for p in self._predecessors(task_id))

    def _missing_predecessors(self, task_id: str) -> list[str]:
        return [p for p in self._predecessors(task_id) if self.ctx.state_of(p) != "accepted"]

    # ------------------------------------------------------------ 三集合
    def eligible(self) -> list[str]:
        """计划允许且未终态。"""
        return self._ordered([
            t.task_id for t in self.plan.tasks
            if self.ctx.state_of(t.task_id) not in ("cancelled",)
        ])

    def ready(self) -> list[str]:
        """当前处于 ready 且前驱仍满足。

        注意：`waiting` 需要先由 `reconcile()` 提升，调度器不会自动把 blocked 捞起来——
        契约要求 blocked 经人工命令回到合法状态。
        """
        return self._ordered([
            t.task_id for t in self.plan.tasks
            if self.ctx.state_of(t.task_id) == "ready" and self._predecessors_accepted(t.task_id)
        ])

    def reconcile(self) -> list[str]:
        """把前驱已全部验收的 waiting 任务提升为 ready，并写事件。返回本次提升的任务。"""
        promoted: list[str] = []
        with self._lock:
            for t in self.plan.tasks:
                if self.ctx.state_of(t.task_id) != "waiting":
                    continue
                if not self._predecessors_accepted(t.task_id):
                    continue
                self.ctx.transition(
                    t.task_id, "ready",
                    reason="前驱产物已验收",
                    actor="Zeus", actor_type="program",
                )
                promoted.append(t.task_id)
        return self._ordered(promoted)

    def conflicts(self, task_id: str) -> list[str]:
        """与当前在途许可的冲突原因；空列表表示可派。"""
        task = self.tasks[task_id]
        reasons: list[str] = []
        load = self._executor_load.get(task.executor, 0)
        if load >= self.executor_capacity:
            holder = next((p.task_id for p in self._permits.values()
                           if p.executor == task.executor), "?")
            reasons.append(f"执行者 {task.executor} 已被 {holder} 占用")
        for permit in self._permits.values():
            if set(task.resources) & set(permit.resources):
                shared = sorted(set(task.resources) & set(permit.resources))
                reasons.append(f"资源键 {shared} 被 {permit.task_id} 占用")
            for mine in task.write_scope:
                for other in permit.write_scope:
                    if _paths_overlap(mine, other):
                        reasons.append(f"写范围冲突 {mine} ↔ {other}（{permit.task_id}）")
        return reasons

    def dispatchable(self) -> list[tuple[str, list[str]]]:
        """[(task_id, 冲突原因)]，按优先级排名排序；原因为空即可派。

        关键：这里是**模拟占位**而不是逐个独立判断。同一执行者只能承担一个任务，
        如果对 T02/T03/T04 各自判断"此刻能否派"，三个都会显示可派——那是错的。
        按优先级顺序模拟占用，后面被前面挡住的要如实显示原因，界面才不会给出
        "三个都能点、点下去两个失败"的假象。
        """
        with self._lock:
            if self.ctx.run.status != "active":
                return [(tid, [f"run 状态为 {self.ctx.run.status}，不可派发"]) for tid in self.ready()]

            sim_permits: dict[str, Permit] = dict(self._permits)
            sim_load: dict[str, int] = dict(self._executor_load)
            out: list[tuple[str, list[str]]] = []
            for tid in self.ready():
                if len(sim_permits) >= self.max_parallel:
                    out.append((tid, ["并发上限已满"]))
                    continue
                task = self.tasks[tid]
                reasons: list[str] = []
                if sim_load.get(task.executor, 0) >= self.executor_capacity:
                    holder = next((p.task_id for p in sim_permits.values()
                                   if p.executor == task.executor), "?")
                    reasons.append(f"执行者 {task.executor} 已被 {holder} 占用")
                for permit in sim_permits.values():
                    shared = sorted(set(task.resources) & set(permit.resources))
                    if shared:
                        reasons.append(f"资源键 {shared} 被 {permit.task_id} 占用")
                    for mine in task.write_scope:
                        for other in permit.write_scope:
                            if _paths_overlap(mine, other):
                                reasons.append(f"写范围冲突 {mine} ↔ {other}（{permit.task_id}）")
                if not reasons:
                    sim_permits[tid] = Permit(
                        task_id=tid,
                        generation=self.ctx.runtime(tid).generation,
                        executor=task.executor,
                        write_scope=tuple(task.write_scope),
                        resources=tuple(task.resources),
                    )
                    sim_load[task.executor] = sim_load.get(task.executor, 0) + 1
                out.append((tid, reasons))
            return out

    # ------------------------------------------------------------ 原子占用
    def acquire(self, task_id: str) -> Permit | None:
        """原子检查 + 占位 + 迁移到 running。拿不到返回 None（原因见 dispatchable）。"""
        with self._lock:
            if self.ctx.run.status != "active":
                return None
            if self.ctx.state_of(task_id) != "ready":
                return None
            if not self._predecessors_accepted(task_id):
                return None
            if len(self._permits) >= self.max_parallel:
                return None
            if self.conflicts(task_id):
                return None

            task = self.tasks[task_id]
            count = self._dispatch_count.get(task_id, 0)
            permit = Permit(
                task_id=task_id,
                generation=self.ctx.runtime(task_id).generation + (1 if count else 0),
                executor=task.executor,
                write_scope=tuple(task.write_scope),
                resources=tuple(task.resources),
            )
            # 占位与状态迁移在同一个临界区，中间不释放锁
            if count:
                self.ctx.commit(
                    reason=f"重新派发第 {count + 1} 次",
                    actor="Zeus", actor_type="program",
                    mutate=lambda snap: _bump_generation(snap, task_id),
                    task_id=task_id,
                )
            self.ctx.transition(
                task_id, "running",
                reason=f"派发第 {count + 1} 次（generation {permit.generation}）",
                actor="Zeus", actor_type="program",
            )
            self._permits[task_id] = permit
            self._dispatch_count[task_id] = count + 1
            self._executor_load[task.executor] = self._executor_load.get(task.executor, 0) + 1
            return permit

    def release(self, task_id: str, *, keep_resources: bool = False) -> None:
        """释放许可。`keep_resources=True` 用于结果未知：资源与执行者都不退还。"""
        with self._lock:
            permit = self._permits.get(task_id)
            if permit is None:
                return
            if keep_resources:
                return
            del self._permits[task_id]
            self._executor_load[permit.executor] = max(
                0, self._executor_load.get(permit.executor, 1) - 1)

    def submit(self, task_id: str, *, reason: str = "产物已冻结，等待检查与复核",
               actor: str = "Hephaestus") -> None:
        """产物提交：先释放执行槽，再转 verifying。

        提交即释放，是因为产物已冻结、不再占用执行者；验收另占复核者资源。
        这条规则让"六个人"能跑 8 个任务，否则整条链被验收速度卡死。
        """
        with self._lock:
            self.release(task_id)
            self.ctx.transition(task_id, "verifying", reason=reason,
                                actor=actor, actor_type="agent")

    # ------------------------------------------------------------ 派发 + 执行替身
    def dispatch(self, task_id: str, executor: FakeExecutor) -> FakeExecutor | None:
        """取许可 → 交给执行器 → 记下 attempt。拿不到许可返回 None。"""
        preflight = getattr(executor, "validate_dispatch", None)
        if preflight is not None:
            preflight(task_id)
        permit = self.acquire(task_id)
        if permit is None:
            return None
        attempt = executor.dispatch(task_id, permit.generation)
        self._attempts[task_id] = attempt.attempt_id
        self.ctx.commit(
            reason=f"发送许可 {attempt.attempt_id}（generation {permit.generation}）",
            actor="Zeus", actor_type="program",
            mutate=lambda snap: None,
            task_id=task_id, attempt_id=attempt.attempt_id,
        )
        return executor

    def permit_for(self, task_id: str) -> Permit | None:
        """Read-only P4 integration point; acquisition remains scheduler-owned."""
        with self._lock:
            return self._permits.get(task_id)

    def attempt_for(self, task_id: str) -> str | None:
        with self._lock:
            return self._attempts.get(task_id)

    def accept_submission(self, task_id: str, generation: int,
                          artifacts: list[str],
                          *, checks_passed: list[str] | None = None) -> tuple[str, str]:
        """原子接纳一次执行结果。返回 `(status, reason)`。

        issue #5 要求"原子接纳检查**任务/输入/报告**、取消/改版与当前执行代际"，
        所以这里一次检查五件事：

        | 检查 | 不通过时 |
        | --- | --- |
        | 任务仍在执行中 | `rejected` |
        | 代际未过期（迟到回执） | `stale` |
        | 有产物 | `rejected` |
        | 输入产物都已验收 | `rejected`（`task.inputs` 比对已验收任务的 outputs） |
        | 程序检查证据齐全 | `rejected`（`checks_passed` 必须覆盖 `task.checks`） |
        """
        with self._lock:
            runtime = self.ctx.runtime(task_id)
            task = self.tasks[task_id]

            if runtime.state != "running":
                return "rejected", f"{task_id} 当前 {runtime.state}，不在执行中"
            if generation != runtime.generation:
                return "stale", (
                    f"代际过期：产物 generation={generation}，"
                    f"当前 generation={runtime.generation}")
            if not artifacts:
                return "rejected", "没有产物不能接纳"

            missing_inputs = self.missing_inputs(task_id)
            if missing_inputs:
                return "rejected", f"输入未就绪：{missing_inputs}"

            missing_checks = [c for c in task.checks if c not in (checks_passed or [])]
            if missing_checks:
                return "rejected", f"缺少程序检查证据：{missing_checks}"

            self.submit(task_id)
            return "accepted", f"{task_id} 产物已冻结，等待复核"

    def missing_inputs(self, task_id: str) -> list[str]:
        """任务声明的输入里，还没被任何已验收任务产出的那些。

        契约把"需要哪个已验收产物"写在 `task.inputs`，与 `depends_on` 不是同一件事：
        一个任务的直接前驱，可能不是它需要的那份产物的产出者。
        """
        task = self.tasks[task_id]
        if not task.inputs:
            return []
        produced: set[str] = set()
        for other in self.plan.tasks:
            if self.ctx.state_of(other.task_id) == "accepted":
                produced.update(other.outputs)
        return [item for item in task.inputs if item not in produced]

    def settle_unknown(self, task_id: str, *, verified_running: bool = False) -> None:
        """结果未知的处置：保持资源占用，直到核实或明确隔离。

        `verified_running=True` 表示已向提供方确认仍在运行——仍保留资源；
        只有核实结束（成功/失败/确认取消）才 `release`。
        """
        with self._lock:
            if self.ctx.state_of(task_id) != "blocked":
                self.ctx.transition(task_id, "blocked", reason="结果未知，待核实",
                                    actor="Zeus", actor_type="program")
            if not verified_running:
                return

    # ------------------------------------------------------------ 局部异常登记
    def register_incident(self, task_id: str, kind: IncidentKind, reason: str,
                          *, needs_human: bool = True) -> Incident:
        """登记一条局部异常。只记账，不动任务状态。

        调用方随后决定：技术失败走 `failed`，需要人等走 `blocked`。
        两者都不影响**其他支线**继续派发——这是"局部"的含义。
        """
        with self._lock:
            return self._incidents.add(task_id, kind, reason, needs_human=needs_human)

    def resolve_incident(self, task_id: str, note: str) -> None:
        with self._lock:
            self._incidents.resolve(task_id, note)

    def open_incidents(self) -> list[Incident]:
        with self._lock:
            return self._incidents.open()

    def unsettled_inflight(self) -> list[str]:
        """结果未知、尚未核实的在途任务——它们仍占着资源。

        issue #6："暂停后不新增许可，在途仍可接纳收尾；**未知未核实不显示全部已停**"。
        所以"暂停"不等于"已停干净"：还有人占着执行者时，界面不能显示绿色的"全部停止"。
        """
        with self._lock:
            unsettled: list[str] = []
            for task_id in self._permits:
                if self.ctx.state_of(task_id) != "blocked":
                    continue
                kinds = [i.kind for i in self._incidents.open_for(task_id)]
                if "outcome_unknown" in kinds:
                    unsettled.append(task_id)
            return unsettled

    def cancel(self, task_id: str, *, reason: str = "人工取消",
               actor: str = "YOU") -> tuple[str, str]:
        """取消任务。返回 `(status, reason)`。

        取消与验收的竞争只允许一个合法迁移：
        - 已 `accepted` 的任务不能取消（终态不可覆盖）；
        - 已 `cancelled` 的任务不接受产物（`accept_submission` 会拒）。
        """
        with self._lock:
            state = self.ctx.state_of(task_id)
            if state in ("accepted", "cancelled"):
                return "rejected", f"{task_id} 已是终态 {state}，不能取消"
            self.release(task_id)
            self.ctx.transition(task_id, "cancelled", reason=reason,
                                actor=actor, actor_type="human")
            return "accepted", f"{task_id} 已取消，后继不可用"

    # ------------------------------------------------------------ 投影
    def projection(self) -> dict:
        from olympus.projections.runtime.projection import build_runtime_projection
        return build_runtime_projection(self)


def _bump_generation(snapshot: Run, task_id: str) -> None:
    runtime = next(t for t in snapshot.tasks if t.task_id == task_id)
    runtime.generation += 1


def _paths_overlap(left: str, right: str) -> bool:
    a = left.replace("\\", "/").rstrip("/")
    b = right.replace("\\", "/").rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")

# P3 交付与 P4 接棒

更新：2026-11-15。对应 issue [#5](https://github.com/kyrie21z/SWE-Olympus/issues/5)（动态调度）
与 [#6](https://github.com/kyrie21z/SWE-Olympus/issues/6)（人工命令框架）。

本文按 issue 的"PR 完成与交接"要求写：**合并提交、启动/检查命令、接口/样例、已知限制、
mock/真实验证级别**。清单未勾选不代表完成。

## 1 交付范围

| issue | 模块 | 已提供内容 |
| --- | --- | --- |
| #5 | `orchestrator/scheduler/` | 运行态三集合、`priority_rank` 排序、原子派发与资源/执行者互斥、取消、执行替身、局部异常登记 |
| #5 | `orchestrator/state_machine/` | Run 快照与受控迁移、事件与快照同事务、8 状态合法边、改版失效 |
| #5 | `projections/runtime/` | 状态摘要、accepted n/N、阻塞原因、动态资源占用、待人工异常 |
| #6 | `commands/core/` | 鉴权、内容版本、幂等键、CAS、处理器注册、`allowed_actions` |
| #6 | `commands/registry/` | 动作 → 允许角色、前置状态、P5 待接管清单 |
| #6 | `workflow/` | LangGraph 阶段控制：准备 → 持久化等待 → 原子接纳 |
| #6 | `human/` | 人工请求的持久化存储；**绑定 REQ/DS/PLAN Ref 与必要报告**，报告不齐拒绝开关口 |

关口的串行与预算语义（`tests/scenarios/gates/`）：

- `test_resume_does_not_recharge`：恢复不消耗模型调用、`state_revision` 只前移一格、事件只写一条；
- `test_concurrent_resume_is_serialized`：三个线程同时 resume，接纳只发生一次（`Gate` 内部锁）；
- `test_new_version_requires_new_confirmation`：内容变更后旧请求标 stale，新稿必须新请求，不继承旧签认。

暂停语义（`projections/runtime`）：`pause_state.fully_stopped` 只在"没有未核实在途"时为真。
有任务处于 `outcome_unknown` 且未核实、仍占着执行者时，界面显示 `unsettled=[...]`，
不能报绿色的"全部已停"（`test_unknown_inflight_prevents_fully_stopped`）。

**不重复实现**：静态拓扑、层级、优先级索引、静态冲突索引来自 P2 的 `CompiledManifest`；
本段只在运行态叠加"现在能不能派"的判定。

## 2 启动与检查

```bash
pip install -r requirements.lock
python -m pytest tests/orchestrator tests/scenarios/scheduler -q   # 28 passed
python -m pytest -q                                               # 209 passed
python -m ruff check backend/olympus/orchestrator backend/olympus/projections \
                     backend/olympus/commands tests/orchestrator \
                     tests/scenarios/scheduler scripts/p3_verify.py  # All checks passed
python scripts/p3_verify.py    # 跑测试 + 走一遍人工关口 + 生成可贴 issue 的证据
```

`scripts/p3_verify.py` 的产物 `P3-验收输出.md` 不进版本库（已在 `.gitignore`）。

## 3 接口

### 3.1 状态机

```python
RunContext(run: Run)
  .run                                  # 当前快照（只读使用）
  .events                               # 已产生事件，按 event_seq 递增
  .runtime(task_id) -> TaskRuntime
  .state_of(task_id) -> str
  .commit(*, reason, actor, actor_type, mutate, task_id=None, attempt_id=None) -> Event
  .transition(task_id, new_state, *, reason, actor, actor_type="program", verification=None) -> Event
  .set_run_status(status, *, reason, actor) -> Event
  .snapshot_view() -> dict
```

`commit` 是唯一写入口：改快照 → `state_revision`/`event_seq` 各 +1 → 生成带快照的 `Event`。
`transition` 会先过 `assert_transition`，非法边抛 `IllegalTransition`。

### 3.2 调度

```python
Scheduler(plan, manifest, ctx, max_parallel=3, executor_capacity=1)
  .eligible() / .ready() / .dispatchable()      # 三集合；dispatchable 返回 (task_id, 冲突原因[])
  .reconcile() -> list[str]                     # waiting → ready（前驱全 accepted）
  .acquire(task_id) -> Permit | None            # 原子：状态/前驱/并发/执行者/写范围/资源
  .release(task_id, *, keep_resources=False)    # 结果未知时保留资源
  .submit(task_id)                              # 产物冻结：释放执行槽 → verifying
  .dispatch(task_id, executor) -> executor | None
  .accept_submission(task_id, generation, artifacts, *, checks_passed=None) -> (status, reason)
  .missing_inputs(task_id) -> list[str]   # 输入产物是否都已验收
  .settle_unknown(task_id, *, verified_running=False)
  .cancel(task_id, *, reason="人工取消", actor="YOU") -> (status, reason)
  .register_incident(task_id, kind, reason, *, needs_human=True) -> Incident
  .resolve_incident(task_id, note)
  .open_incidents() -> list[Incident]
  .projection() -> dict
```

### 3.3 命令

```python
CommandBus(ctx, scheduler)
  .dispatch(Command) -> Receipt      # 六道闸门：run 归属 → 幂等 → 内容版本 → 目标版本 → 权限 → 前置状态
  .catalog() -> list[dict]           # action / actors / pre / effect
  .notes: dict[str, str]             # operation_id → 人类可读原因（Receipt 没有 detail 字段）
```

## 4 样例：一条完整时间线

```python
from olympus.commands.core.bus import CommandBus
from olympus.contracts.models import Command, ContractBundle
from olympus.orchestrator.scheduler import FakeExecutor, Scheduler
from olympus.orchestrator.state_machine import RunContext
from olympus.planning.service import PlanService

bundle = ContractBundle.model_validate_json(
    open("fixtures/contracts/valid.json", encoding="utf-8").read())
package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)

ctx = RunContext(run=bundle.run)
sched = Scheduler(plan=bundle.plan, manifest=package.manifest, ctx=ctx)
bus = CommandBus(ctx=ctx, scheduler=sched)

bus.dispatch(Command(operation_id="op-1", run_id=ctx.run.run_id, actor="YOU",
                     action="start", target_id=ctx.run.run_id, expected_version=1,
                     expected_state_revision=0, parameters={}))
sched.reconcile()                                  # T01 → ready

executor = FakeExecutor()
sched.dispatch("T01", executor)                     # 占许可 + 派发 + 记 attempt
attempt = sched._attempts["T01"]
executor.finish(attempt, "succeeded", ["A01"])      # 执行器说成功（等价退出码零）

sched.accept_submission("T01", generation=1, artifacts=["A01"])   # → verifying，不是 accepted
ctx.transition("T01", "accepted", reason="Argus 复核通过", actor="Argus",
               actor_type="agent", verification="passed")
sched.reconcile()                                  # T02/T03/T04 → ready

sched.projection()["progress"]                     # {'accepted': 1, 'required': 8}
sched.projection()["sets"]["blocked"]              # 谁被谁挡住，含原因
```

## 5 已知限制

| 限制 | 影响 | 归属 |
| --- | --- | --- |
| `RunContext` 只在进程内，不落库 | 进程重启后要由 P5 用事件 + 快照重建 | P5 #9 |
| 单进程一把锁 | 多进程并发恢复未有互斥 | P5 #9 |
| `allowed_actions(run)` 未实现 | 界面暂时自己拼"能做什么" | #6 待补 |
| `retry / reassign / accept_risk / request_change` | 已按 #6 改为 `NOT_READY`，P5 用 `bus.register()` 接管 | P5 #9 |
| 关口的图状态用 `InMemorySaver` | 同进程重放可用；跨进程要换持久化 saver | P5 #9 |
| 无 HTTP、无前端 | 只提供 `projection()` 与 `Receipt` 两个可序列化出口 | P5/P6 |
| `FakeExecutor` 是脚本化替身 | 不能证明真实工具行为 | P4 #8 |

## 6 验证级别

- 模型：**未调用**。调度、命令、状态机都是程序逻辑，测试不触达 LLM。
- 执行：**mock**。`FakeExecutor` 回放 `running/succeeded/failed/outcome_unknown`，
  不启动真实进程、不写真实文件。
- 数据：`fixtures/contracts/valid.json` 里的构造计划（8 任务 12 边），
  与 P2 的 `PlanService` 编译产物一致。
- 因此本轮能证明的是**状态机与调度规则按设计执行**，不能证明真实执行器的可靠性。

## 7 给下一位的接入点

| 接棒人 | 怎么接 |
| --- | --- |
| P4 #8 | 用真实执行器替换 `FakeExecutor`：实现 `dispatch/query/finish/cancel` 四个方法即可；`Artifact`/`Review` 落地后，`accept_submission` 改由"产物入库"事件驱动 |
| P5 #9 | `projection()` 直接挂 `/api/state`；`Receipt.model_dump(mode="json")` 直接当 `/api/command` 响应；恢复时读 `RunContext.events` 的 `snapshot` 重建 |
| P5 #9 | 接手 `retry / reassign / accept_risk / request_change` 四个处理器，向 `commands/registry/actions.py` 注册即可，不改核心核 |
| P6 #11/#12 | 界面四视图读 `projection()`：`sets` / `progress` / `resources` / `tasks` / `incidents` |

## 8 PR 完成清单

- [x] 同段两项验收分节提供证据（`docs/p3/融合说明.md` 第八节 + `P3-验收输出.md`）
- [x] 交接含启动/检查命令、接口/样例、已知限制、mock 级别（本文 2–6 节）
- [x] 无密钥、无越界写入；`P3-验收输出.md` 已进 `.gitignore`
- [ ] 当前 PR HEAD 的检查（`ruff` + `pytest`）——本地已全绿，待 CI 复核
- [ ] 非作者复核（P4）
- [ ] `Closes #5` `Closes #6`

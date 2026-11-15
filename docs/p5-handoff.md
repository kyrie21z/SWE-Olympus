# P5：有界恢复、修复处理器与薄 API 交接说明

基准：主分支提交 `cc0929a`（P4 的 #20 合入后）。
对应任务：OL-09（#9）、OL-10（#10）。
阶段分支：`feat/p5-recovery-api`，两个 Issue 共用同一个 PR。

本文按 Issue 的"PR 完成与交接"要求写：**合并提交、启动/检查命令、接口/样例、
已知限制、mock/真实验证级别**。清单未勾选不代表完成。

## 1 交付范围

| 模块 | 已提供内容 |
| --- | --- |
| `api/` | 只读快照、事件补取、聚合视图、追踪、执行详情、命令提交、命令痕迹、能力声明；统一错误信封 |
| `projections/http/` | 视图组装：P2 追踪 + P3 摘要/`allowed_actions` + P4 执行详情 |
| `orchestrator/recovery/` | 运行态注册表、事件重建与核对、上游修复、四处中断核对、有界重试策略 |
| `agents/momus/failure/` | 失败归因的离线实现（显式标注 heuristic） |
| `commands/runtime_handlers/` | `retry` / `reassign` / `accept_risk` / `request_change` |
| `commands/runtime/` | 命令服务与持久化插槽（`CommandJournal` / `NullJournal`） |
| `tests/api/`、`tests/recovery/`、`tests/scenarios/recovery/` | 接口、恢复与失败路径场景 |
| `fixtures/http/`、`docs/api/`、`scripts/export_api_fixtures.py` | 确定性请求/响应样例与接口说明 |

**不重复实现**：发送账本、许可/代际、执行查询与取消全部复用 P2/P3/P4 的接口；
本段只在它们之上做组装与传输。

## 2 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/runs/{run_id}` | 权威 Run 快照（先恢复运行态，再返回） |
| GET | `/api/runs/{run_id}/events?after_seq=N` | 补齐该序号之后的事件 |
| GET | `/api/runs/{run_id}/view` | 聚合视图：快照 + P3 摘要 + `allowed_actions` + 追踪 |
| GET | `/api/runs/{run_id}/trace` | P2 的 REQ→DS→Task 追踪矩阵 |
| GET | `/api/runs/{run_id}/executions/{task_id}` | P4 的执行/工件详情（宿主注入查询服务） |
| POST | `/api/commands` | 提交命令，返回 `Receipt` |
| GET | `/api/runs/{run_id}/trail` | 命令痕迹：重试提示、风险理由、改派目标、修复记录 |
| GET | `/api/capabilities` | 当前持久化与恢复能力声明 |
| GET | `/health` | 基础健康检查，`stage=P5-api` |

命令响应带两个头：

```text
X-Olympus-Durability: memory        # 本次操作是否已落库
X-Olympus-Recorded-Events: 0        # 实际持久化的事件条数
```

错误统一为：

```json
{"error": {"code": "RUN_NOT_FOUND", "message": "...", "detail": {}}}
```

| code | HTTP | 含义 |
| --- | --- | --- |
| `RUN_NOT_FOUND` | 404 | 运行不存在 |
| `INVALID_CURSOR` | 422 | `after_seq` 为负 |
| `CURSOR_AHEAD` | 409 | 游标超过服务端已提交序号，必须重取快照 |
| `RECOVERY_GAP` | 503 | 事件链与快照对不上，拒绝提供半成品状态 |
| `PLAN_NOT_BOUND` | 409 | 运行态存在但未绑定已批准计划，不能执行命令 |
| `TRACE_UNAVAILABLE` | 409 | 缺少需求/设计原文，无法构建追踪 |
| `EXECUTION_VIEW_UNAVAILABLE` | 409 | 宿主未注入 P4 执行查询服务 |
| `EXECUTION_NOT_FOUND` | 404 | 任务或尝试没有执行记录 |
| `INVALID_ARGUMENT` | 422 | 请求体或参数不合法 |

## 3 恢复语义

`rebuild_context` 只接受**可证明完整**的链条，四条核对全部通过才返回：

1. 事件条数等于快照 `event_seq`；
2. 事件序号从 1 连续递增；
3. `state_revision` 只允许 +0（签认类）或 +1；
4. 末条事件的 `snapshot` 与当前快照逐字段一致。

任一条不满足抛 `RecoveryGap`，HTTP 映射为 `503 RECOVERY_GAP`，**不返回半截状态**。

四处中断的核对结果（`RestartReconciler`）：

| 中断点 | 判据 | 处置 |
| --- | --- | --- |
| 发送前 | 账本 `prepared` | 本地取消，`calls_consumed=0` |
| 发送后未回 | 账本 `sent`/`outcome_unknown` | 有查询能力则查；查不到保留未知、预算继续占用 |
| 工件已保存未接纳 | 任务 `verifying` | 保持未验证，等待独立验收 |
| 已接纳 | `accepted` 且验收 `passed` | 保持已验收，不重跑、不复核 |

**核对不等于重发**：恢复过程不产生新请求，`outcome_unknown` 在核对前禁止重发。

## 4 失败处理

归因优先级（`HeuristicFailureAttributor`，`mode="heuristic"`）：

```text
结果未知 → unknown（先查询） → 设计不符 → design → 越界写入 → task
→ 缺上游产物 → upstream → 检查失败 → task → 无证据 → unknown
```

重试策略（`BoundedRetryPolicy`）：

| 归因 | 动作 |
| --- | --- |
| 本任务 | `retry`，首次 + 最多 2 次 |
| 上游 | `repair`，不消耗被阻塞任务自己的额度 |
| 设计 / 测试 | `human` |
| 归因不明 | `human` |
| 结果未知且未查询 | `query`（禁止重发） |
| 查询后仍未知 | `human`（预算保持占用） |

修复子任务 `Txx-R1`（`plan_repair`）一次完成：

1. 旧验收失效：`accepted → waiting`、`stale`、代际 +1（先离开 accepted，再标 stale）；
2. 前驱不再验收的 `ready` 任务降回 `waiting`；
3. 原任务转 `cancelled`，理由写明"已被修复任务取代"；
4. `Run.tasks` 追加修复任务，事件记录计划变更；
5. 计划前进一版，**直接后继改依赖修复任务**；
6. 替换调度器的 `plan` / `manifest`，许可与尝试记录保持不动。

前驱取上游任务自己的前驱，并显式排除自身与全部后继；同一原任务默认只允许
一个自动修复，额外修复需 `allow_additional=True`（人工授权）。

## 5 已知限制

| 限制 | 影响 | 归属 |
| --- | --- | --- |
| 命令事件未落库（`NullJournal`） | 进程重启后回退到最近一次已提交快照；响应头 `X-Olympus-Durability: memory` 明确标注 | 待 P1 确认批量写入接口后接入 `StoreJournal` |
| `Store.commit` 一次只能写一条事件 | `start` 等命令经 `reconcile()` 产生多条事件，无法用现有接口原子写入 | 同上 |
| 单进程运行态 | 多进程并发恢复无互斥 | 后续验证 |
| `supersedes` / `failure_ref` 存在 P5 运行态 | 契约 `Task` 没有对应字段，未擅自扩展 | 契约演进时迁入 |
| 修复失效事件由 P5 以 `program` 记录 | 未复用 `invalidate_revision`，因为后者把 `actor_type` 固定为 `human` |  P3 可考虑增加 actor_type 参数 |
| `CommandBus._persist` 未使用 | 该方法未被任何测试覆盖，且与 `Store.commit` 的计数器契约不一致；本段未走该路径 | P3 后续修复 |
| LangGraph 关口状态仍用 `InMemorySaver` | 同进程重放可用；跨进程需持久化 saver。当前 HTTP 层走 `CommandBus`，未经过该图，因此不影响本段链路 | 若后续把关口接入 API，单独立项并补依赖 |
| 归因是启发式 | 不能代替在线 Momus 语义复核 | 接入模型后补充 |
| 未接真实执行器 | 修复/重试场景使用构造运行态与脚本化结果 | P4 适配器已提供接口 |

## 6 验证级别

- 模型：**未调用**。归因与命令处理都是程序规则。
- 执行：**mock**。修复与中断场景使用构造运行态，不启动真实进程。
- 数据：`fixtures/contracts/valid.json` 的八任务十二边；SQLite 临时库。

本地结果（可复现）：

```powershell
python scripts/project.py check    # 369 passed；Ruff 通过；前端 18 passed；构建成功
python scripts/export_api_fixtures.py --check  # 录制夹具无漂移
python scripts/project.py scenario # 含 tests/scenarios/recovery/
python scripts/project.py browser  # 由 CI 执行，本地未安装 Chromium
```

CI：PR #21 的 `P1 foundation checks / check` 通过，覆盖 `check` 与浏览器测试。

## 7 给 P6 的接入点

| P6 视图 | 直接读 | 注意 |
| --- | --- | --- |
| 整体 | `GET /api/runs/{id}/view` | 一次取齐快照、摘要、`allowed_actions`、追踪 |
| 阶段进度 | `view.snapshot.status` 与 `view.projection.progress` | 不要用颜色判断成功 |
| 任务依赖图 | `view.snapshot.tasks` + `events` | 缺号时按 `needsRefetch` 重取 |
| Agent 泳道 | `GET /api/runs/{id}/trail` + `GET .../executions/{task}` | 提示与理由与验证分开显示 |
| 追踪矩阵 | `view.trace` 或 `GET .../trace` | 缺失时明确显示"追踪不可用" |
| 统一命令面板 | `POST /api/commands` | 读 `X-Olympus-Durability`，不要乐观标绿 |

## 8 提交与合并

- PR：`#21`，`Draft` → 完成后转 `Ready for review`
- `Closes #9`、`Closes #10`
- 非作者评审：P6
- 合并前复核 P4 留下的前段兼容修改（`docs/p4-handoff.md` 第五节）

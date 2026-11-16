# P6 交付与联调说明

更新：2026-11-16。对应 issue [#11](https://github.com/kyrie21z/SWE-Olympus/issues/11)
与 [#12](https://github.com/kyrie21z/SWE-Olympus/issues/12)。

## 1 已实现范围

| issue | 模块 | 实现 |
| --- | --- | --- |
| #11 | `frontend/src/p6/P6ControlConsole.tsx` | 单运行控制台、阶段、accepted n/N、并发和阻塞提示 |
| #11 | `frontend/src/p6/P6Dag.tsx` | 服务端 manifest 层、关键链、节点状态和任务详情 |
| #12 | `frontend/src/p6/P6ControlConsole.tsx` | Agent 泳道、追踪表、统一命令面板、执行证据 |
| #11/#12 | `frontend/src/data/http/client.ts` | P5 HTTP 快照、事件、聚合视图、追踪、执行和命令 |
| #11/#12 | `frontend/src/data/core/run-view.ts` | `RunView`、`allowed_actions`、执行详情和回执类型 |
| #11/#12 | `frontend/src/p6/model.ts` | HTTP 视图适配、DAG 布局、命令参数和覆盖结论 |
| #11/#12 | `frontend/tests/data/p6.test.ts` | 视图投影、风险、DAG、allowed_actions 和命令参数测试 |
| #11/#12 | `frontend/tests/e2e/p6.spec.ts` | 任务证据、重复命令、回执和手机布局 |
| 本地联调 | `scripts/p6_demo_server.py` | 真实 P5 API + 临时 SQLite + 绑定 PLAN/追踪 |

页面入口：

```text
http://127.0.0.1:5175/?run=RUN-campus#console
```

默认通过 Vite `/api` 代理访问 `127.0.0.1:8000`。部署到其他后端时，在
`frontend/.env` 设置：

```text
VITE_API_BASE_URL=https://your-olympus-backend.example
```

## 2 P5 接口接入

| 页面能力 | 接口 |
| --- | --- |
| 运行快照 | `GET /api/runs/{run_id}` |
| 事件补取 | `GET /api/runs/{run_id}/events?after_seq=N` |
| 四视图统一数据 | `GET /api/runs/{run_id}/view` |
| 命令痕迹 | `GET /api/runs/{run_id}/trail` |
| 任务执行与工件 | `GET /api/runs/{run_id}/executions/{task_id}` |
| 人工命令 | `POST /api/commands` |

页面首屏读取 `/view`，不读取 `scenarios.json`。阶段、accepted 分子、阻塞原因、
任务层级、关键链和 `allowed_actions` 都直接使用服务端字段。

事件轮询从当前 `event_seq` 开始，调用 P1 的 `mergeEvents`：

- 缺号或修订号断裂时，`needsRefetch=true`，重新读取 `/view`。
- 重复事件不重复应用。
- 断线时显示“数据可能过期”，不倒退展示。

## 3 四视图

### 阶段进度

- accepted n/N 来自 `view.projection.progress`。
- 阶段等待、进行、阻塞和最终验收只消费后端摘要。
- paused 状态显示等待人工。

### 只读业务 DAG

- 层和关键链来自 `view.projection.manifest`。
- 任务状态、执行者和复核者来自 `view.projection.tasks`。
- 修复任务不在静态 `standard_plan.json` 时，由 `view.snapshot.tasks` 与
  `view.repairs` 动态补充。
- 页面不允许拖动依赖，也不把重试边混入业务 DAG。

### Agent 泳道与执行详情

- 泳道按服务端 `priority_rank` 分组。
- 选中任务后读取 execution API，展示工具、diff、检查输出、工件和复核请求。
- execution API 不可用时显示明确的 `EXECUTION_VIEW_UNAVAILABLE`，不填假工件。

### 追踪表

- 直接展示 `view.trace.rows`。
- 只有服务端 `accepted=true` 且 `verification_status=passed` 时显示已验证。
- disposition 为 `risk_accepted` 时显示“未验证 · 人工接受”。

## 4 统一命令

- 可选项和禁用原因来自 `view.allowed_actions`。
- 页面不自行判断 run/task 是否允许动作。
- `retry` 传 `prompt`。
- `accept_risk` 传 `reason`。
- `reassign` 传 `assignee`。
- `request_change` 传 `level` 与 `summary`。
- 每个命令生成稳定 `operation_id`，支持“再次提交同一 operation”。
- 回执显示 HTTP 头 `X-Olympus-Durability` 和
  `X-Olympus-Recorded-Events`。

## 5 验证

```powershell
cd frontend
npm test
npm run build
npm run test:browser -- --workers=1
```

覆盖：

- P5 `view.json` 契约回放。
- 事件回放与任务执行详情。
- `allowed_actions` 驱动命令可用性。
- 同一 operation 重复提交。
- 命令 durability 回执。
- 桌面与 390px 手机布局。

本地结果（2026-11-16）：

- `npm test`：24 passed。
- `npm run build`：TypeScript 和 Vite 构建通过。
- P6 E2E：2 passed；全套 E2E：6 passed。
- `scripts/project.py check`：369 个后端测试、24 个前端数据测试、Ruff、契约和构建通过。
- 使用 `p6_demo_server.py` 启动真实 `/view` 接口后，浏览器真实 API 冒烟通过。

## 6 已知限制

| 项目 | 当前状态 |
| --- | --- |
| 命令持久化 | P5 返回 `X-Olympus-Durability: memory`，重启可能回退 |
| 执行详情 | 需要宿主注入 P4 `ArtifactAcceptanceService`，否则 API 明确返回不可用 |
| 真实模型 | 当前契约验收使用 mock/真实工具边界，未证明在线模型质量 |
| 后端运行数据 | 页面要求 `OLYMPUS_DB` 中存在目标 run，且运行态已绑定计划 |
| 轮询 | 当前为 5 秒事件补取，不是 SSE/WebSocket |

## 7 演示材料

- [P6 证据索引](demo/p6-evidence-index.md)
- [P6 演示脚本](demo/p6-demo-script.md)

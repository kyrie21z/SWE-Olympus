# HTTP 接口

对应 OL-10（#10）。所有路由都只是包装 P2/P3/P4/P5 的查询服务，**不重新推导业务规则**。

协议版本 `schema_version="1.0"`。启动：

```powershell
python scripts/project.py backend   # http://127.0.0.1:8000
```

交互式文档：`/docs`。

## 端点

| 方法 | 路径 | 返回 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/runs/{run_id}` | `Run` | 权威快照。先重建运行态，再返回 |
| GET | `/api/runs/{run_id}/events?after_seq=N` | `Event[]` | 该序号之后的事件，按序递增 |
| GET | `/api/runs/{run_id}/view` | 聚合视图 | 快照 + P3 摘要 + `allowed_actions` + 追踪 |
| GET | `/api/runs/{run_id}/trace` | `PlanningTraceabilityIndex` | 需求 × 设计 × 任务矩阵 |
| GET | `/api/runs/{run_id}/executions/{task_id}?attempt_id=` | 执行/工件详情 | 由宿主注入的 P4 查询服务提供 |
| GET | `/api/runs/{run_id}/trail` | 命令痕迹 | 重试提示、风险理由、改派、修复记录 |
| POST | `/api/commands` | `Receipt` | 提交命令 |
| GET | `/api/capabilities` | 能力声明 | durability / recovery / 限制 |
| GET | `/health` | 健康检查 | `stage=P5-api` |

命令响应头：

```text
X-Olympus-Durability: memory   # 本次操作是否已落库
X-Olympus-Recorded-Events: 0   # 实际持久化的事件条数
```

## 错误格式

```json
{"error": {"code": "RUN_NOT_FOUND", "message": "...", "detail": {}}}
```

| code | HTTP | 触发条件 |
| --- | --- | --- |
| `RUN_NOT_FOUND` | 404 | 运行不存在 |
| `INVALID_CURSOR` | 422 | `after_seq` 为负 |
| `CURSOR_AHEAD` | 409 | 游标超过已提交序号，必须重取快照 |
| `RECOVERY_GAP` | 503 | 事件链与快照对不上，拒绝半成品状态 |
| `PLAN_NOT_BOUND` | 409 | 未绑定已批准计划，不能执行命令或出摘要 |
| `TRACE_UNAVAILABLE` | 409 | 缺少需求/设计原文，无法构建追踪 |
| `EXECUTION_VIEW_UNAVAILABLE` | 409 | 宿主未注入执行查询服务 |
| `EXECUTION_NOT_FOUND` | 404 | 任务或尝试没有执行记录 |
| `INVALID_ARGUMENT` | 422 | 请求体或参数不合法 |

## 样例与回放

确定性的请求/响应样例在 `fixtures/http/`：

| 文件 | 内容 |
| --- | --- |
| `capabilities.json` | 能力声明 |
| `snapshot.json` | 已提交暂停事件的运行快照 |
| `events.json` | `after_seq=0` 的事件补取结果 |
| `trace.json` | 追踪矩阵 |
| `view.json` / `view_after_start.json` | 聚合视图 |
| `command_start.json` | 一次被接纳的命令（含 durability 头） |
| `command_stale.json` | 陈旧修订号被拒绝的回执 |

生成或校验：

```powershell
python scripts/export_api_fixtures.py          # 重新生成
python scripts/export_api_fixtures.py --check  # CI 可用：漂移返回非零
```

录制时只抹掉 `event_id` 与 `occurred_at`，其余逐字节稳定，可直接作为 P6 的
传输替身：把 `request` 当作输入、`status/headers/body` 当作响应即可。

## 限制

- 命令事件目前不落库（`X-Olympus-Durability: memory`）；重启后回退到最近一次已提交快照。
- 执行/工件详情需要宿主注入 P4 的 `ArtifactAcceptanceService`；未注入时明确返回
  `EXECUTION_VIEW_UNAVAILABLE`，不返回空对象。
- 追踪需要需求与设计原文；只有计划时返回 `TRACE_UNAVAILABLE`。
- 单进程运行态；不做多进程并发恢复。

# P6 证据索引

| 验收点 | 代码 / 证据 |
| --- | --- |
| 单运行控制台与阶段 | `frontend/src/p6/P6ControlConsole.tsx` |
| accepted n/N 来自后端 | `view.projection.progress`，P6 数据测试 |
| 业务 DAG、关键链 | `P6Dag.tsx`、`buildDagLayoutFromView()` |
| 任务执行、工具、diff、检查、工件 | `GET /executions/{task_id}` + `P6ControlConsole.tsx` |
| 追踪表和风险接受 | `view.trace`、`deriveTraceRowsFromView()` |
| allowed_actions 驱动命令 | `commandAvailabilityFromView()` |
| 重复命令同回执 | `createCommandForView()` + “再次提交同一 operation” |
| 事件补取与重取 | `mergeEvents()` + 5 秒轮询 |
| 桌面和手机 | `docs/preview/p6-console.png`、`docs/preview/p6-mobile.png` |
| 真实 P5 API 联调 | `scripts/p6_demo_server.py` + `frontend/test-results/p6-live-api.png` |

测试命令：

```powershell
cd frontend
npm test
npm run build
npm run test:browser -- --workers=1
```

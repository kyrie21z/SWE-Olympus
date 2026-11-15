# P2交付与P3接棒

日期：2026-11-15。对应Issue #3（OL-03）、#4（OL-04），并在同一分支 `feat/p2-agents-planning` 完成端到端接线。未合并前不关闭Issue。

## 本段交付

| Issue | 实现 | 验收入口 |
| --- | --- | --- |
| #3 | 统一Agent Runner、角色权限与输入白名单、输出审计、Mock/真实provider适配、请求账本（发送前预留、幂等键、未知结果保留预算）、执行记录与推理剥离、通用独立复核（作者/角色隔离、版本与输入指纹失效、发现关闭权限） | tests/agents、tests/reviews |
| #4 | Daedalus八任务十二边规划与单次修订、Momus程序校验加语义复核、PlanningWorkflow、任务卡、静态manifest编译器（分层/拓扑/关键路径启发式/静态冲突）、REQ→DS→Task追踪索引、PlanService打包 | tests/planning、fixtures/plans |
| 接线 | Daedalus/Momus改为通过 `AgentRunner` 调用：计划与复核分别以 `plan`、`plan_review` 记账并写入 `ExecutionRecordService`；Momus同时向 `CommonReviewer` 提交 `ReviewSubmission`；`close_blockers` 同步关闭已记录发现 | tests/scenarios/test_p2_planning_and_review.py |

规划调用写入 `LOG-PLAN-{REQ}` / `PLAN-{REQ}`，复核调用写入 `LOG-REVIEW-{PLAN}-V{版本}`；`fixture_key` 为 `Daedalus:plan`、`Momus:plan_review`。追踪矩阵只表示“已规划”，`verification_status` 固定 `unverified`、`accepted` 固定 `False`；计划包 `approved` 与 `execution_started` 同样固定 `False`。

## 验证入口

- `python scripts/project.py check`：契约/规划夹具漂移、Ruff、154项后端测试、前端契约检查、前端测试与构建。
- `python scripts/project.py scenario`：无界面场景，当前3项，包含 `tests/scenarios/test_p2_planning_and_review.py`。
- `python scripts/project.py generate`：改动公共模型或规划语义后重新生成 Schema、TypeScript 与 `fixtures/contracts`、`fixtures/plans`；不要手改生成物。
- 场景使用固定 REQ `I001`、DS `DS-I001` 与 `MockProvider`，不需要模型凭据，也不访问网络。
- 上述检查证明 mock 链路、账本、复核与静态编译可复现，不证明真实模型质量、动态调度或自动开发已实现。

## 后续接线

1. P3把 `AgentRunner` 接入Zeus调度：任务执行前按 `RolePermissions` 声明写入范围与输入键，调用后以账本状态判定可重试性；`outcome_unknown` 必须先对账，不得直接重发。
2. P3的人工关口读取 `PlanningOutcome.reviews`、`CommonReviewer` 记录与 `PlanPackage`，但不能把 `verify_for_acceptance` 或 `revisions_used` 当作真人批准；批准需独立命令与人工确认记录。
3. P3排程使用 `CompiledManifest` 的分层、拓扑序与静态冲突索引作为约束；关键路径是启发式，不得当作工期承诺或调度事实。
4. P4接入正式REQ与Athena设计：复用同一 `CommonReviewer`（`target_kind="DS"`，复核人Momus），在真实调用前完成契约、来源、确认人权限与依赖核验。
5. P4接入真实provider时通过环境变量注入，凭据不写入仓库；缺配置应保留 `ProviderNotConfiguredError`，不能回退到伪造成功。
6. P5用 `ExecutionRecordService.project_for_api` 暴露记录，用 `RequestLedger.query/cancel` 支持恢复与补取；补取结果必须凭provider证据改判。
7. P6直接消费追踪矩阵与manifest投影，保持只读；命令面板的每个动作走服务端命令与回执，不在前端乐观改状态。

## 安全与隔离边界

- **统一入口**：已接入的角色调用都经 `AgentRunner`；`task_kind`、输入键、`input_refs` 在发送前校验，白名单外的输入直接拒绝。
- **输出审计**：模型输出不得写 `confirmation`/`confirmed_by`、`call_budget`/`calls_used`，不得把 `state` 标为 `accepted`、把 `verification_status` 标为 `passed`；`write_scope`、`path` 等路径必须落在声明的允许范围内。
- **账本先行**：请求在发出前预留调用额度；同一幂等键不得重复发送，未知结果保留预算并标记待对账，不静默记为失败或免费调用。
- **推理不外泄**：provider 输出与工具结果中的 `reasoning`、`thinking` 等字段在入库前递归剥离。
- **复核独立**：作者不得自审，实现角色（Athena/Daedalus/Hephaestus）不得担任复核人，PLAN与DS只能由Momus复核；复核目标必须绑定当前版本与内容指纹，`input_refs` 变化即失效。
- **通过条件**：`passed` 需要 `required_checks` 全部满足且没有未关闭的 blocker；Daedalus永远不能关闭自己的阻塞项，`close_blockers` 只接受Momus或显式授权的非作者。
- **单次修订**：PlanningWorkflow最多修订一次，无法修复的问题（环路、越界、粒度）保留到修订版并停下等待人工。
- **结论不被改写**：关闭发现只更新 `CommonReviewer` 内的发现状态，不会把已记录的 `blocked` 结论改成 `passed`；新结论必须重新复核并记录新的review。
- **Mock与真实模型**：`MockProvider` 只按固定夹具返回，结果可复现；mock 输出的 `verdict`、任务版本与指纹不参与判决，计划版本与内容指纹由程序生成。`RealProvider` 需 `OLYMPUS_MODEL_API_KEY`、`OLYMPUS_MODEL_BASE_URL`、`OLYMPUS_MODEL_NAME` 三者齐全，缺失即拒绝；传输失败或5xx记为未知结果。
- **完整计划载荷**：模型返回完整 `Plan` 载荷时会先通过输出审计；由于计划任务自带 `write_scope`，角色需同时声明允许的写入范围，否则调用被拒绝。当前mock只返回计划大纲，未覆盖该真实路径。

## 文件位置

- 运行时：`backend/olympus/agents/runtime/`；账本：`backend/olympus/requests/ledger.py`；通用复核：`backend/olympus/reviews/common/`。
- 规划：`backend/olympus/agents/daedalus/planner.py`、`backend/olympus/agents/momus/planning/reviewer.py`、`backend/olympus/planning/`、`backend/olympus/traceability/planning/`。
- 夹具：`fixtures/agents/mock_responses.json`（含 `Daedalus:plan`、`Momus:plan_review`）、`fixtures/plans/`。
- 生成脚本：`scripts/export_contracts.py`、`scripts/export_plans.py`；`--check` 失败即表示生成物漂移。

P1的契约、存储与文件交接边界见 [p1-handoff](p1-handoff.md)，本段不改变其结论。

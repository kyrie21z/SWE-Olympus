# P2交付与P3接棒

更新：2026-11-15。Issue #3/#4的基础实现已由PR #17合入main；本次修订对应审查中发现的五类缺口。以下描述当前代码的接口和验证边界，不以Issue已关闭代替验收。

## 1. 交付范围

| 模块 | 已提供内容 |
| --- | --- |
| Runtime | AgentRunner、RoleRunInput/RoleResult、权限和输入白名单、输出审计、Mock/RealProvider、执行记录 |
| 请求与复核 | RequestLedger、发送前预留、query/cancel协议；CommonReviewer非作者隔离、版本绑定与关闭权限 |
| 规划 | Daedalus语义任务接入、程序生成版本/指纹、任务卡、Momus发现合并和有限返修 |
| 静态服务 | PlanService、拓扑/分层/前后继/层级启发式、静态冲突索引、REQ→DS→Task矩阵 |
| 构造验证 | 校园八任务十二边、反例、单次返修及文档检索/版本历史业务走查 |

本期不实现Zeus动态派发、正式REQ授权核验、真实执行器、恢复/API或前端四视图；分别由P3～P6接入。

## 2. 本次修订

1. 不完整或错误模型输出抛OutputSchemaError，保留调用与失败记录，不静默套用校园计划。
2. Daedalus和Momus读取完整REQ/DS正文及任务，来源仍绑定精确Ref；Momus模型发现参与最终判决并进入CommonReviewer。
3. 校验plan.requirement、plan.design、design.requirement；失配产生STALE_INPUT_REFERENCE，禁止打包或带输入编译，追踪索引也拒绝。
4. sent或outcome_unknown请求无提供方取消确认时，保持outcome_unknown、cancel_requested、预算预留与待对账；prepared可本地取消。
5. 区分未来任务的write_scope建议与当前调用实际写权限；合法计划可进入，越界路径、实际path/artifact写入及特权字段仍被拒绝。

## 3. Daedalus输入输出

AgentRunner输入的manifest包含requirement/design精确Ref、requirement_content、design_content、范围外限制、提示词与output_schema。模型只需提出tasks语义数组；每项包含type、goal、sources、inputs/outputs、depends_on、executor/reviewer、write_scope、checks、resources、priority、policy与required等现有Task语义字段。

程序构造Task/Plan、初始version=1和SHA-256，绑定当前REQ/DS。完整Plan输出也可接收，存在输入引用时必须匹配；模型提供的版本/指纹不会作为新计划的权威。后续内容修订通过revise_plan生成新版本。调用新计划时的业务版本登记/去重与批准由P3/P4服务完成，本模块不推断全局最新版本。

RolePermissions.can_propose_task_scopes仅允许Daedalus的plan任务使用，只豁免输出根tasks[i].write_scope的实际写授权检查。仍检查路径安全性；不赋予工具写文件权限，不豁免path、artifact.location或确认/预算/accepted等写入。未来执行时由P3/P4独立核验范围。

未提供Runner时，build_plan只读取fixtures/contracts/valid.json中的准确REQ/DS版本对应计划。内置MockProvider的规划/审查响应由export_plans生成，绑定REQ/DS Ref；不匹配的业务输入在发送前拒绝。其他业务使用真实provider或显式配置匹配输入的测试响应，不借用课程模板。

## 4. Momus与收敛

Momus模型输出需提供verdict和findings/ issues列表。结构化发现包含code、severity（blocker/important）、可选task_id和message；程序校验任务引用和报告目标（如提供），并设置raised_by与当前Plan绑定。非空文字发现也会保守转换成问题，未知格式或无理由的阻断声明明确报错。

程序发现与模型发现共同决定最终结论：存在blocker则blocked，其他发现则needs_revision，没有发现才passed；模型写passed不能覆盖实际问题。报告包含执行记录引用及真实provider模式。长问题说明保留全文。

没有Runner时只执行程序/启发式检查，证据标记momus:heuristic-review，不冒充独立模型语义复核。mock模式下模型结论也是构造数据，不证明真实模型质量。

PlanningWorkflow最多一次内容修订；未解决问题继续阻断并停在blocked_awaiting_human。确定性revise_plan只修复可定位的程序问题，不保证能自动解决含义争议。无效模型输出不触发无限重试，向调用者抛OutputSchemaError；P3根据既有预算决定格式纠正或人工处理。作者不能自行关闭发现，关闭问题也不会把旧blocked报告改成passed。

## 5. 版本与取消边界

打包和编译在当前输入下重新检查三条依赖绑定；不要仅凭需求编号相同、任务无环或先前passed复用旧包。静态包和追踪矩阵始终不携带人工批准或执行成功。

RequestLedger.cancel：
- prepared且尚未发送：可直接取消，未消耗模型调用。
- sent且无提供方确认：转outcome_unknown，保留额度、取消请求及待对账标记。
- 提供方明确cancelled：记录确定终态及实际调用；迟到响应不覆盖取消终态。
- 未知/未确认：不能向用户表示已终止，不能盲目重发。

query/cancel的hook由对应provider/工具适配器提供证据，本地账本本身不会向远端发送取消。P5必须消费真实查询结果，不将请求已提出等同于终态。

## 6. 后续接线

| 接棒人 | 应消费的接口与责任 |
| --- | --- |
| P3 | Runner、PlanPackage、CompiledManifest、审查报告；实现当前版本登记、预算/执行代际、资源许可、人工确认和动态调度 |
| P4 | 正式REQ授权与来源校验、Athena；复用Runner/CommonReviewer；执行前独立检查任务范围与证据 |
| P5 | 账本query/cancel、ExecutionRecordService.project_for_api；接恢复、提供方证据及HTTP，不重新拼业务判据 |
| P6 | manifest/矩阵/动态状态/回执，只读展示；统一命令走后端，不乐观标绿 |

预算、权限、确认记录只能由程序/真人管理。请求账本可选JSON持久化，执行记录和CommonReviewer目前仍为进程内服务；完整恢复持久化和多用户并发接线尚待后续验证。不要把这些服务当作生产级多进程平台。

## 7. 验证与位置

- python scripts/project.py check：契约/规划夹具漂移、Ruff、全部后端测试、前端数据检查与构建。
- python scripts/project.py scenario：固定案例、有限返修及另一业务的无界面走查。
- tests/planning/test_handoff_regressions.py：本次反例回归、范围隔离、模型发现/无效输出、依赖失效、取消与HTTP替身传输。
- python scripts/project.py browser：既有文件交接、刷新恢复、旧草稿和手机布局，默认5175；测试可设置OLYMPUS_TEST_PORT。
- 规划模型响应生成物：fixtures/agents/planning_responses.json；修改后运行generate，提交生成差异。

HTTP替身验证了RealProvider的传输适配与正文传递，没有请求真实模型。当前证据不覆盖在线模型质量、真实执行或完整产品链路；数量与运行结果见对应PR。P1范围见[p1-handoff](p1-handoff.md)。
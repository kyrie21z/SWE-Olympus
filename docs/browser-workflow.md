# 网页工作流和 Agent API 配置

网页现在通过持久化宿主连接需求接纳、设计、规划、实际 Python 执行和最终验收。各阶段使用现有 Agent Runner、独立审查和调度器；用户的批准决定是否进入下一阶段。

## 启动

在仓库根目录分别运行：

```powershell
python scripts/project.py backend
python scripts/project.py frontend
```

打开 http://127.0.0.1:5175 。正式入口使用 `.olympus/state.sqlite3`；可通过环境变量 `OLYMPUS_DB` 修改数据库位置。已有的 `p6_demo_server.py` 仍用于构造数据演示，日常工作流使用上述 backend 入口。

## 设置模型接口

点击顶栏“API 设置”或进入“协作角色”，为 Athena、Daedalus、Momus、Hephaestus、Argus、Hermes 分别填写 API 根地址、模型名称、API Key 和超时。支持 OpenAI 兼容的 Chat Completions 协议，根地址后自动追加 `/chat/completions`。例如服务根地址为 `https://api.example.com/v1`，不要填写完整的 completions 路径。

先保存配置，再点击“测试连接”。测试向已保存的接口发送一次最小请求。各角色可使用同一服务或不同服务。Zeus 是程序编排器，YOU 是人工批准者，不需要模型配置。

配置保存在当前数据库的独立表中。API Key 留空表示保留原密钥，勾选“清除已保存密钥”即可删除。浏览器只显示密钥是否已保存。

没有任何完整的模型 API 配置时，工作台默认使用离线模式。离线支持[库存预占样例](../fixtures/requirements/inventory-reservation/requirement.json)和项目中心的[团队文档样例](../frontend/public/examples/requirement.json)的原始版本。Agent 响应来自按需求指纹匹配的预置材料，Python 检查和工件仍在本机实际生成。其他需求请配置 API 后重试。

完整保存任一模型接口后，新运行选择在线模式。在线模式会报告缺少配置的具体角色。设计阶段已开始调用模型后，后续阶段继续使用该次运行的模式。此前因缺少配置且尚未发出请求的设计，可以点击“重试当前阶段”，按最新配置重新选择模式。

## 从需求到验收

1. 在项目中心导入有效 REQ JSON 并保存。可使用 `fixtures/requirements/inventory-reservation/requirement.json`。构造需求须明确勾选允许用于测试。
2. 在工作台点击“建立运行并核验需求”，再点击“接纳此需求用于当前运行”。服务端记录绑定当前版本和指纹的确认，构造来源继续保持构造标识。
3. 点击“生成设计规格”。Athena 根据需求生成设计，Momus 独立审查；最多进行一次自动内容返修。阅读设计正文和报告后，点击“批准当前设计”。
4. 点击“生成任务计划”。Daedalus 规划任务，Momus 审查，并在实现开始前生成各任务的冻结验收脚本。阅读计划、依赖、角色和检查脚本，勾选已审阅，再批准计划。
5. 点击“执行全部任务”。Zeus 按依赖派发计划中的全部任务；每项任务执行真实 Python 检查并由 Argus 独立复核。实现任务由 Hephaestus 生成授权范围内的文件；Momus、Hermes 的测试、集成和记录任务执行冻结检查并生成实际报告。
6. 查看逐任务执行证据，或打开“查看此运行控制台”，阅读实际状态、DAG、追踪和命令回执。全部任务被接纳后，由用户确认最终验收。

MD/TXT 保持为原文材料，需要整理成有效 REQ JSON 后才能生成设计。导入记录与后端运行通过工作流编号关联，刷新页面会恢复该关联。生成和执行在后台运行，页面轮询状态，不要求保持当前标签页开启。

## 持久化和故障处理

命令接口默认使用 `StoreJournal`，响应头 `X-Olympus-Durability: sqlite`。一次事务保存整批事件、最新快照、命令回执和运行态绑定；同一 `operation_id` 重放复用已保存的结果，更改载荷则冲突。REQ、DS、PLAN、调度许可、执行尝试、改派与修复线索也会恢复。

工作流阶段、批准、设计、计划、冻结检查及执行证据另行保存；模型与工具请求均保留账本。重启不会自动重发在途请求。服务在生成或执行期间意外停止时，页面显示需要核对；`outcome_unknown` 保持未知，不伪装为已取消或已失败。已知未配置或确定失败的生成阶段可在修正设置后显式重试；重试使用新的操作编号并保留历史调用计数。

暂停后不会继续派发新任务。任务失败时展示检查输出或独立复核阻断原因；可在对应运行控制台进行适用的人工处置，再继续执行。受控执行使用单进程、本地 Python 的现有课程执行配置，不包含多语言、分布式执行或生产部署。

## 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/settings/agents` | 查询六个角色的公开配置 |
| PUT | `/api/settings/agents/{role_id}` | 保存角色配置 |
| POST | `/api/settings/agents/{role_id}/test` | 测试已保存的连接 |
| POST | `/api/workflows` | 从 REQ 建立运行 |
| GET | `/api/workflows/{workflow_id}` | 读取阶段、文档、批准及证据 |
| POST | `/api/workflows/{workflow_id}/actions` | 提交带操作编号的阶段动作 |

人工动作依次为 `approve_requirement`、`generate_design`、`approve_design`、`generate_plan`、`approve_plan`、`execute`、`accept_run`。错误采用统一信封；阶段返回允许动作，客户端不通过颜色或已有文档猜测是否可以执行。

## 验证范围

`tests/workflows/test_browser_host.py` 使用本地 OpenAI 兼容 HTTP 替身、真实设置接口、真实 Agent Runner、SQLite 和真实 Python 子进程走完八任务计划，检查全部任务接纳、执行详情、P6 视图及重建应用后的恢复。它验证传输和工作流接线，在线模型的实际输出质量需用用户配置的提供方另行验证。

前端继续保留 API 契约回放测试，同时测试各角色配置、人工关口和页面恢复；实际 HTTP 与执行服务的联调不能由回放测试替代。

不使用 API 回放的完整浏览器验证命令：

```powershell
$env:PLAYWRIGHT_CHANNEL='chrome' # 本机已有 Chrome 时使用；CI 使用已安装的 Chromium
.venv\Scripts\python scripts/verify_browser_workflow.py
```

该检查启动独立的 Vite、FastAPI、临时 SQLite 及本地 HTTP 模型替身，在浏览器里保存和测试 API 设置、导入需求、依次操作全部人工关口，完成八任务实际执行和 P6 的 8/8 展示。它不写入日常数据库，结束时清理自有进程，报告和截图位于 `.olympus/browser-workflow-*/`。CI 已加入此检查。测试专用 Vite 可通过 `OLYMPUS_BACKEND_URL` 指向隔离后端，默认仍为本机8000端口。

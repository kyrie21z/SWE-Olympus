# SWE-Olympus

面向软件工程课程的多 Agent 工作流原型。项目由 **6 人、8 周**协作完成，目标是把上一阶段已确认的需求文件，逐步转成设计规格、开发任务、可视化编排与验收记录。

业务内容由输入文件决定，平台不绑定校园报名或其他固定题目。我们在这一阶段重点实现“设计规格 → 任务分解 → 编排器 → 执行可视化与交互”，通过需求文件接续上一阶段的需求确认工作。

> **当前进度（2026-11-15）：** `main` 已包含 P1 工程基础和文件交接页面壳。公共契约与 SQLite 存储已在 P1 数据版本实现，但尚未进入 `main`。`feat/p2-agents-planning` 在数据版本之上加入 P2 运行时、规划与复核能力：统一 Agent Runner、请求账本、执行记录、通用独立复核、Daedalus 任务分解、Momus 计划复核、静态 manifest 编译与追踪矩阵，配套端到端场景。真实模型调用、设计生成、动态调度和完整可视化仍待后续成员接入。

## 导航

- [项目流程](#workflow)
- [当前可用能力](#status)
- [技术架构与角色](#architecture)
- [本地启动](#quick-start)
- [需求文件怎么传](#file-input)
- [页面使用](#console)
- [目录与数据约定](#structure)
- [检查与测试](#checks)
- [六人分工与提交方式](#collaboration)
- [常见问题](#faq)
- [文档与交付入口](#references)

<a id="workflow"></a>

## 1. 项目流程

```mermaid
flowchart LR
    REQ["需求文件 REQ"] --> IN["导入与正式接纳"]
    IN --> DS["Athena：设计规格 DS"]
    DS --> PLAN["Daedalus：任务计划 PLAN"]
    PLAN --> REVIEW["Momus 检查 / YOU 确认"]
    REVIEW --> RUN["Zeus 编排 / 受控执行"]
    RUN --> ACCEPT["独立复核 / 真人验收"]
```

上图描述目标流程。当前页面已经提供文件入口和阶段展示，后续节点仍为待接入状态。

| 产物 | 解决的问题 | 主要内容 |
| --- | --- | --- |
| REQ：需求规格 | 为什么做、做什么、如何判断满足需求 | 需求条目、验收条件、范围外限制、来源和确认记录 |
| DS：设计规格 | 需求如何落实到软件设计 | 模块、数据、接口、状态、并发、异常、交互和设计决策 |
| PLAN：任务计划 | 开发工作如何拆分、依赖和验收 | 任务卡、依赖关系、执行者、复核者、输入输出和检查规则 |
| Run：运行记录 | 当前执行到哪里，为什么等待或失败 | 任务状态、调用尝试、事件、工件、检查结果与人工操作 |
| 验收证据 | 结果是否满足当前版本的要求 | 真实程序检查、独立复核报告及真人验收记录 |

工作流中，**模型提出和生成内容，程序控制规则与状态，真人负责关键取舍和确认**。模型自评通过、程序退出码为零或页面显示绿色，都不能单独替代正式验收。

<a id="status"></a>

## 2. 当前可用能力

请先区分以下代码，避免将分支上的能力当成主分支已经具备：

| 能力 | 当前 `main` | P1 数据版本 | P2 规划分支 |
| --- | --- | --- | --- |
| Python/FastAPI/LangGraph 基础与健康检查 | 已有 | 已有 | 已有 |
| React/Ant Design 控制台、手机布局 | 已有 | 已有 | 已有 |
| JSON/Markdown/TXT 文件读取、原文预览与导出 | 已有 | 已有 | 已有 |
| JSON 语法检查 | 已有 | 已有 | 已有 |
| REQ 字段、重复编号、内容指纹和确认目标检查 | 待接入 | 已有，本地预检 | 已有，本地预检 |
| 结构化需求条目、来源和确认声明展示 | 待接入 | 已有 | 已有 |
| 刷新后恢复导入记录、导出旧版 Prompt 草稿 | 待接入；当前仅页面会话 | 已有，浏览器本地保存 | 已有，浏览器本地保存 |
| 公共模型、同源 JSON Schema/TypeScript | 待接入 | 已有 | 已有 |
| SQLite 事务存储、事件和幂等回执 | 待接入 | 已有，供受信服务层调用 | 已有 |
| 统一 Agent Runner、请求账本、执行记录 | 待实现 | 待实现 | 已有，mock 可复现 |
| 通用独立复核（CommonReviewer）与发现关闭权限 | 待实现 | 待实现 | 已有 |
| Daedalus 任务分解、Momus 计划复核、单次修订 | 待实现 | 待实现 | 已有，固定夹具 |
| 静态 manifest 编译与 REQ→DS→Task 追踪矩阵 | 待实现 | 待实现 | 已有，可导出夹具 |
| 真实模型调用与凭据管理 | 待实现 | 待实现 | 适配器已具备，需环境变量，未验证 |
| 正式 REQ 接纳、Athena 设计生成 | 待实现 | 待实现 | 待实现 |
| 动态编排、真实执行、恢复和完整四视图 | 待实现 | 待实现 | 待实现 |

**分支状态说明：** [PR #14](https://github.com/kyrie21z/SWE-Olympus/pull/14) 已合入 `main`；[PR #15](https://github.com/kyrie21z/SWE-Olympus/pull/15) 已合入 `feat/p1-01-foundation`，其数据增量尚未合回 `main`；P2 的两项 Issue #3、#4 已在 `feat/p2-agents-planning` 合并为分支增量，同样尚未合回 `main`。因此，#15 显示“已合并”不代表主分支已有全部 P1 数据能力，“已合并”也不代表 P2 能力已经进入主分支。完整数据版本与规划分支可按下文独立检出体验。

本期不包含生产部署、多租户、任意命令执行平台、Nyx 自动复盘或长期智能记忆。已有构造案例用来验证协议和边界，不代表平台只支持该业务，也不代表真实模型流程已跑通。

<a id="architecture"></a>

## 3. 技术架构与角色

### 3.1 技术选型

| 层次 | 技术 | 当前用途 |
| --- | --- | --- |
| 前端 | React、TypeScript、Vite、Ant Design、Lucide | 文件交接、项目导航、只读预览和流程占位 |
| 服务基础 | Python 3.12、FastAPI | 健康检查与后续服务入口 |
| 工作流 | LangGraph | 当前验证最小状态图；完整阶段图由后续模块实现 |
| 数据契约 | Pydantic、JSON Schema、TypeScript、AJV | 数据版本已实现同源定义与前端预检 |
| 状态存储 | SQLite | 数据版本已实现事务快照、事件和操作回执 |
| 质量检查 | Ruff、pytest、Vitest、Playwright、GitHub Actions | 静态检查、数据与状态验证、浏览器检查 |

依赖版本以 [Python 锁文件](requirements.lock) 与 [前端锁文件](frontend/package-lock.json) 为准；安装时优先复现锁定版本。

目标分层为：**页面消费服务状态 → 服务校验命令 → Zeus/Harness 控制流程 → Agent/工具完成任务 → 存储记录状态与证据**。当前前端在浏览器内完成文件预览，后端只提供健康检查；页面尚未通过 HTTP 接入完整运行服务。

### 3.2 角色职责

以下是设计中的职责分工，页面角色卡片不代表对应模型任务已经运行。

| 角色 | 类型 | 职责 |
| --- | --- | --- |
| Athena | Agent | 从有效 REQ 生成设计规格，保持需求来源和范围边界 |
| Daedalus | Agent | 拆分任务，明确依赖、输入输出、资源和验收条件 |
| Momus | Agent | 检查设计/计划的遗漏、越界、边界场景及验收定义 |
| Hephaestus | Agent | 在授权范围内实施任务，提交工件、变更和执行记录 |
| Argus | Agent | 独立复核执行产物与检查证据，不由执行者自行批准 |
| Hermes | 协作角色 | 组织集成、产物交接和交付记录；本期不扩展为自动生产部署 |
| Zeus / Harness | 程序控制层 | 控制任务状态、依赖、资源许可、预算、事件和命令接纳 |
| YOU | 真人 | 补充真实信息、处理关键取舍、批准阶段产物并最终验收 |

神话名称用于识别职责；输出仍采用普通工程文档和结构化数据。增加一个角色名，不等于已经实现一个独立服务。

<a id="quick-start"></a>

## 4. 本地启动

### 4.1 环境要求

- Git，并具备本仓库的访问权限。
- Python **3.12**：与当前 CI 的验证环境一致。
- Node.js **22.12 或更高版本**及 npm。
- P1 本地演示和检查**不需要模型 API Key**；首次安装依赖需要联网。

以下命令均在仓库根目录执行，除非明确进入 `frontend`。

```text
git clone https://github.com/kyrie21z/SWE-Olympus.git
cd SWE-Olympus
```

### 4.2 Windows / PowerShell

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock
npm ci --prefix frontend
python scripts/project.py check
```

### 4.3 macOS / Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
npm ci --prefix frontend
python3 scripts/project.py check
```

统一脚本会优先使用仓库内的 `.venv`，无需先激活环境。macOS/Linux 后续命令如没有 `python`，请使用 `python3`。

### 4.4 启动前后端

在两个终端中分别运行：

```text
python scripts/project.py backend
```

```text
python scripts/project.py frontend
```

- 前端：[http://127.0.0.1:5175](http://127.0.0.1:5175)
- 健康检查：[http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)
- FastAPI 接口文档：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

健康检查应返回 `status: ok`、`stage: P1-foundation`、`model_required: false`。这只表示基础服务可用，不表示 Agent 或编排器正在运行。两个终端分别使用 `Ctrl+C` 停止服务。

### 4.5 体验完整 P1 数据版本

如需体验 REQ 预检、格式样例和刷新恢复，建议在另一个目录单独检出，避免混淆主分支：

```text
git clone --branch feat/p1-02-contracts https://github.com/kyrie21z/SWE-Olympus.git SWE-Olympus-data
cd SWE-Olympus-data
```

随后按 4.2 或 4.3 安装并启动。数据版本与主分支默认使用相同端口，演示时只启动一套，或参考常见问题更换端口。上述分支的已验证代码基线为 `d40ba17`。

如需连同 P2 规划能力一起体验，可改为检出规划分支：

```text
git clone --branch feat/p2-agents-planning https://github.com/kyrie21z/SWE-Olympus.git SWE-Olympus-p2
```

该分支包含 P1 数据契约与 P2 运行时、规划、复核、静态编译和追踪矩阵，所有 Agent 调用都走固定 mock 夹具，不需要模型凭据，也不会访问网络或执行真实命令。

### 4.6 配置说明

[.env.example](.env.example) 保留 `OLYMPUS_MODE` 和 `OLYMPUS_DB` 作为后续接入约定。**当前启动壳没有自动加载该文件，也没有通过这些变量切换真实模型或启动数据库服务。** P1 不要求创建 `.env`；P2 的 `RealProvider` 通过进程环境变量 `OLYMPUS_MODEL_API_KEY`、`OLYMPUS_MODEL_BASE_URL`、`OLYMPUS_MODEL_NAME` 配置，三者缺一即拒绝调用，凭据不得写入仓库。本分支的场景与检查全部使用 mock，未验证真实模型。

<a id="file-input"></a>

## 5. 需求文件怎么传

### 5.1 输入规则

当前主入口接收上一阶段的需求规格 **REQ**。设计规格 **DS** 是后续产物；数据版本会拒绝将 DS 当作 REQ 导入。业务内容保持开放，不在页面预填固定项目。

| 文件格式 | 主分支行为 | 数据版本行为 |
| --- | --- | --- |
| `.json` | 检查 JSON 语法，原文预览，标为待契约校验 | 按 Requirement 契约预检，展开条目与来源 |
| `.md` / `.txt` | 作为待整理材料预览 | 同左，不自动生成已确认需求 |
| PDF / Word / 其他 | 暂不支持解析，会提示换用受支持格式 | 同左 |

文件应为 **UTF-8** 文本，单个文件最多 **512 KB**。空文件、二进制内容、读取失败或无效格式会提示错误。Markdown 按原文展示，不执行其中的 HTML 或指令。不要仅将 PDF/Word 的后缀改成 `.txt`。

### 5.2 REQ JSON 包含哪些信息

完整格式见[可下载样例（数据版本）](https://github.com/kyrie21z/SWE-Olympus/blob/d40ba17cfe07c9d10bb818af8c7bcb27ac4d61d2/frontend/public/examples/requirement.json)。样例为人工构造，只用于格式与交互演示。

| 字段 | 含义 |
| --- | --- |
| `schema_version` / `kind` | 当前契约版本 `1.0`，对象类型 `REQ` |
| `object_id` / `version` / `content_hash` | 需求标识、正整数版本及正文指纹 |
| `content.requirements` | 需求列表；每项包含唯一编号 `id`、内容 `text`、验收条件 `acceptance` |
| `content.out_of_scope` | 明确排除的范围 |
| `provenance` | 来源类别、来源标识及定位信息 |
| `confirmation` | 确认声明，可为空；非空时包含确认人、时间、目标版本/指纹及依赖 |

正式交接文件应由上游按公共契约导出。正文修改后不能沿用旧指纹或旧确认，前端也不会替用户重新签署确认。

### 5.3 校验结果与确认的区别

数据版本会检查字段结构、重复需求编号、正文指纹和确认目标是否匹配，并区分：

- **校验未通过**：不允许保存，应修正文件后重新导入。
- **待整理材料**：文本材料还没有形成结构化 REQ。
- **待补充确认**：有有效结构，但没有确认记录。
- **构造样例**：来源或确认标记为 `synthetic_fixture`，不是实际用户授权。
- **待服务端核验**：本地检查通过并携带确认声明，仍需核验真实性、权限和依赖。

**上传成功、本地预检通过和真人已确认是不同状态。** 当前保存按钮只保存本地记录，设计生成按钮仍禁用；正式接纳由后续服务实现。

### 5.4 保存与版本

主分支仅保留当前页面会话，刷新后清除。数据版本将原文保存在当前浏览器、当前站点，最多 **20 份**，原文总计不超过 **2 MB**，不上传或跨设备同步。

文件原文只读，项目名称和补充说明另存；更换规格创建新导入记录，不覆盖旧文件或自动续用确认。数据版本支持刷新恢复、原文件导出及旧 Prompt 草稿导出；损坏、容量不足或其他页面更新会明确报错。本地存储冲突提示不等于多用户事务。

<a id="console"></a>

## 6. 页面使用

| 页面 | 当前可以做什么 |
| --- | --- |
| 欢迎总览 | 选择/拖入文件、查看预检提示、填写项目名称和补充说明 |
| 项目中心 | 搜索导入记录、按文件进入项目 |
| 项目工作台 | 查看导入内容、导出原文件、浏览后续阶段占位 |
| 协作角色 | 查看各角色的设计职责 |
| 使用指南 | 查看文件格式、来源、确认与流程边界 |

体验顺序：**选择文件 → 查看检查结果 → 保存导入并查看 → 阅读需求/原文 → 导出或导入新版本**。数据版本可以先点击“下载交接样例（构造）”，保存后再选择该文件；主分支暂不提供此按钮。

界面参考 Ant Design Pro 的浅色控制台结构，使用现有 React/Vite 工程实现。当前没有实际任务运行时，进度与产物保持空状态。

数据版本截图：[文件导入页](https://github.com/kyrie21z/SWE-Olympus/blob/d40ba17cfe07c9d10bb818af8c7bcb27ac4d61d2/docs/preview/projects.png) · [规格交接工作台](https://github.com/kyrie21z/SWE-Olympus/blob/d40ba17cfe07c9d10bb818af8c7bcb27ac4d61d2/docs/preview/workstation.png) · [手机布局](https://github.com/kyrie21z/SWE-Olympus/blob/d40ba17cfe07c9d10bb818af8c7bcb27ac4d61d2/docs/preview/mobile.png)。截图展示构造输入，不代表正式生成或运行结果。

<a id="structure"></a>

## 7. 目录与数据约定

当前主分支：

```text
backend/
  olympus/
    app.py                 # FastAPI health
    common/workspace.py    # 工作区路径与临时目录工具
frontend/
  src/
    imports/               # 文件读取、预检入口、原文展示；数据版本替换适配器
    App.tsx                # 控制台页面
    shell-config.tsx       # 导航与角色说明
    main.tsx               # 主题与应用入口
    styles.css             # 页面样式及响应式布局
  tests/e2e/               # 文件交接与手机布局检查
scripts/project.py         # 统一启动与检查命令
tests/
  unit/                    # 工作区与路径工具检查
  scenarios/               # health / LangGraph 最小图场景
.github/                   # CI、Issue/PR 模板
requirements.lock          # Python 依赖锁定
.env.example               # 后续配置约定
```

P1 数据版本额外提供：

```text
backend/olympus/contracts/  # Pydantic 公共对象与规范指纹
backend/olympus/storage/    # SQLite 快照、事件、条件提交和幂等回执
frontend/src/contracts/    # 自动生成的 TypeScript 类型
frontend/src/data/core/    # 客户端协议、事件归并与缺口检查
frontend/src/data/mock/    # 离线数据适配器
frontend/public/examples/  # 可下载的构造 REQ 样例
frontend/tests/data/       # 数据归并、REQ 预检和本地保存检查
fixtures/contracts/        # 合法/非法样例与构造运行场景
scripts/export_contracts.py # 公共契约及样例生成
tests/contracts/           # 契约和版本验证
tests/storage/             # 事务、并发条件与重放验证
docs/                      # 文件交接、公共契约、P1接棒和截图
```

P2 规划分支额外提供：

```text
backend/olympus/agents/runtime/       # Agent Runner、角色权限、provider适配、执行记录
backend/olympus/requests/ledger.py    # 发送前预留的请求账本与幂等键
backend/olympus/reviews/common/       # 通用独立复核与发现关闭权限
backend/olympus/agents/daedalus/      # Daedalus任务分解与单次修订
backend/olympus/agents/momus/planning/ # Momus计划复核与PlanningWorkflow
backend/olympus/planning/             # 语义校验、任务卡、PlanService
backend/olympus/planning/compiler/    # 静态manifest：分层、拓扑、关键路径启发式
backend/olympus/traceability/planning/ # REQ→DS→Task追踪索引
fixtures/agents/                      # mock provider夹具，含计划和计划复核
fixtures/plans/                       # 标准计划、反例、编译manifest和追踪矩阵
scripts/export_plans.py               # 规划夹具生成与漂移检查
tests/agents/ tests/reviews/          # Runner、账本、复核隔离检查
tests/planning/                       # 计划、修订、编译和追踪检查
tests/scenarios/test_p2_planning_and_review.py # 规划到复核的端到端场景
docs/p2-handoff.md                    # P2交付、验证与P3接棒
```

后续接线遵循这些约定：

1. **公共类型同源。** 修改 Python 模型后重新生成 Schema、TypeScript 和夹具，不能分别手改多套定义。
2. **版本精确绑定。** 需求、设计、任务、审查与批准引用对应版本/指纹；不能把旧报告复用为新版本通过。
3. **状态由服务端接纳。** 浏览器消费快照与回执；SQLite 提交接口只面向受信服务层，不能绕过业务权限校验。
4. **证据和风险分开。** 风险接受不等于验证通过；未知请求结果不能记成成功。MockClient 的命令明确返回 `NOT_READY`。
5. **路径工具不等于完整隔离。** P1 提供路径边界检查，后续执行器仍需受控工具和工件复核。

<a id="checks"></a>

## 8. 检查与测试

| 命令 | 当前主分支 | P1 数据版本增量 | P2 规划分支增量 |
| --- | --- | --- | --- |
| `python scripts/project.py check` | Ruff、全部后端测试、TypeScript/Vite 构建 | 增加契约漂移和前端数据检查 | 再增加规划夹具漂移、运行时/规划/复核测试与端到端场景 |
| `python scripts/project.py test` | 后端测试 | 增加前端数据测试 | 后端154项（含3项场景）与前端测试 |
| `python scripts/project.py scenario` | 无界面场景 | 后续成员继续追加自身场景 | 3项：health/LangGraph、规划到复核两条路径 |
| `python scripts/project.py browser` | 文件交接和手机布局 | 增加结构化REQ恢复、旧草稿导出 | 与数据版本相同，未新增浏览器用例 |
| `python scripts/project.py generate` | 暂不提供 | 重新生成公共契约、夹具与TS类型 | 增加标准计划、反例、编译manifest与追踪矩阵 |

第一次执行浏览器检查前：

```text
cd frontend
npx playwright install chromium
cd ..
python scripts/project.py browser
```

也可以使用已安装的 Chrome（PowerShell）：

```powershell
$env:PLAYWRIGHT_CHANNEL='chrome'
$env:OLYMPUS_TEST_PORT='5176'
python scripts/project.py browser
```

测试服务应运行在待验证代码的独立端口，避免误用其他工作区正在运行的页面。CI 使用 Chromium，并执行同一套 `check` 和 `browser` 命令。

| 已验证的代码基线 | 已有检查记录 |
| --- | --- |
| 工程基础 `cdcc340` / PR #14 | 12项后端测试、构建和2项浏览器检查 |
| 数据版本 `d40ba17` / PR #15 | 36项后端测试、18项前端数据测试、构建和4项浏览器检查 |
| 规划分支 `feat/p2-agents-planning` / Issue #3、#4 | 154项后端测试（含3项场景）、前端测试和构建，本机通过；CI结果以该分支最新提交为准 |

两份基线的 GitHub Actions 已通过，最新提交结果请查看 [Actions](https://github.com/kyrie21z/SWE-Olympus/actions)。这些记录验证的是 P1 基础能力、离线交接以及 P2 的 mock 规划与复核链路，不证明真实模型、完整调度或自动开发已经实现。

<a id="collaboration"></a>

## 9. 六人分工与提交方式

每位成员连续完成自己的两项 Issue，再交给下一位继续；每人负责本模块的实现、检查、夹具与交接说明。

| 成员 | Issue | 主要交付 | 初估工作量 | 下一位评审 |
| --- | --- | --- | --- | --- |
| P1 | [#1](https://github.com/kyrie21z/SWE-Olympus/issues/1)、[#2](https://github.com/kyrie21z/SWE-Olympus/issues/2) | 工程骨架、公共契约、状态存储和前端数据基础 | 10 SP | P2 |
| P2 | [#3](https://github.com/kyrie21z/SWE-Olympus/issues/3)、[#4](https://github.com/kyrie21z/SWE-Olympus/issues/4) | Agent Runner、独立复核基础、任务规划、追踪索引和静态编译 | 11 SP | P3 |
| P3 | [#5](https://github.com/kyrie21z/SWE-Olympus/issues/5)、[#6](https://github.com/kyrie21z/SWE-Olympus/issues/6) | Zeus 动态调度、资源许可、状态投影和核心人工关口 | 10 SP | P4 |
| P4 | [#7](https://github.com/kyrie21z/SWE-Olympus/issues/7)、[#8](https://github.com/kyrie21z/SWE-Olympus/issues/8) | 正式 REQ 接纳、Athena 设计、受控执行与工件验收 | 11 SP | P5 |
| P5 | [#9](https://github.com/kyrie21z/SWE-Olympus/issues/9)、[#10](https://github.com/kyrie21z/SWE-Olympus/issues/10) | 有界恢复、运行期命令处理、HTTP API和事件补取 | 11 SP | P6 |
| P6 | [#11](https://github.com/kyrie21z/SWE-Olympus/issues/11)、[#12](https://github.com/kyrie21z/SWE-Olympus/issues/12) | 阶段进度、只读DAG、轻量泳道、追踪表和统一命令面板 | 10 SP | P1 |

SP 是统一尺度的相对工作量估计，不是工时承诺；交付后根据实际耗时和返工校准。现有界面壳为后续接线提供入口，P6仍负责真实数据驱动的四视图和命令交互。各模块的后端缺陷由原负责人修复，不统一留给P6。

### 提交与交接

1. 从包含前置依赖的最新 `main` 建立自己的功能分支，先确认上段交付和检查入口可用。
2. 实现自己两项 Issue 的工作包，随代码提交必要测试、样例与限制说明。
3. 使用 [PR 模板](.github/pull_request_template.md)，关联对应 Issue，说明输入输出、依赖和验证级别。
4. 由下一位成员非作者评审，处理阻断问题后再合并。
5. 交接合并提交、启动命令、公共接口、可复现场景、已知限制以及 mock/真实工具/真实模型的覆盖范围。

P1按约定拆为两个PR；后续成员可沿用Issue中的阶段PR安排。**使用堆叠PR时，基础PR合入main后，应先把后续PR的目标分支改成main并复核，再合并。** 若后续PR已合入旧功能分支，仍需要单独把增量通过PR合回main，不能仅凭“已合并”状态判断交接完成。

<a id="faq"></a>

## 10. 常见问题

| 问题 | 原因与处理 |
| --- | --- |
| 为什么没有“下载交接样例”或刷新后记录消失？ | 当前使用的是主分支基础页面。查看第4.5节体验数据版本，或等待数据增量合回main。 |
| 为什么上传成功后不能生成设计？ | 当前只完成本地交接；正式接纳和生成服务尚未接入，按钮禁用是预期行为。 |
| 只有一段需求 Prompt 怎么办？ | 先在上游完成整理和确认，也可保存为UTF-8的MD/TXT预览；不会自动成为已确认REQ。 |
| 为什么改了JSON后校验失败？ | 数据版本检查正文指纹和确认目标。修改正文后应由上游生成新版本，不能沿用旧指纹/确认。 |
| 文件明明有human确认，为什么仍显示待核验？ | 文件字段不能证明身份或权限，需要后续服务端核验。 |
| PDF/Word上传失败怎么办？ | 当前未接入解析；导出为受支持的文本格式，或由上游生成REQ JSON。 |
| 5173端口已占用怎么办？ | 停止其他开发服务，或运行 `npm run dev --prefix frontend -- --port 5175`。 |
| 提示缺少Python/前端依赖怎么办？ | 在当前检出的仓库重新安装 `requirements.lock` 和执行 `npm ci --prefix frontend`，不要借用其他版本的依赖目录。 |
| 找不到Playwright浏览器怎么办？ | 运行第8节的浏览器安装命令；已有Chrome可设置对应通道。 |
| 前端为什么提示包体积较大？ | 当前构建有主包体积提示，后续可按页面拆分加载；提示本身不等于构建失败，以退出码和CI结果为准。 |

<a id="references"></a>

## 11. 文档与交付入口

| 入口 | 内容 |
| --- | --- |
| [Issue 列表](https://github.com/kyrie21z/SWE-Olympus/issues) | 12项工作包、依赖和验收条件 |
| [Pull Requests](https://github.com/kyrie21z/SWE-Olympus/pulls) | 代码交付、评审与合并进度 |
| [GitHub Actions](https://github.com/kyrie21z/SWE-Olympus/actions) | 各提交的自动检查 |
| [统一脚本](scripts/project.py) | 当前分支实际支持的启动和验证命令 |
| [文件交接说明（数据版本）](https://github.com/kyrie21z/SWE-Olympus/blob/d40ba17cfe07c9d10bb818af8c7bcb27ac4d61d2/docs/input-handoff.md) | 文件格式、预检、保存和正式接纳边界 |
| [公共契约（数据版本）](https://github.com/kyrie21z/SWE-Olympus/blob/d40ba17cfe07c9d10bb818af8c7bcb27ac4d61d2/docs/contracts/README.md) | 对象、指纹、事务及事件语义 |
| [P1交接记录（数据版本）](https://github.com/kyrie21z/SWE-Olympus/blob/d40ba17cfe07c9d10bb818af8c7bcb27ac4d61d2/docs/p1-handoff.md) | 接棒接口、验证及原始交付说明 |
| [P2交接记录](docs/p2-handoff.md) | Agent Runner、请求账本、独立复核、规划与静态编译的交付说明及P3接棒 |
| [规划夹具](fixtures/plans/) | 标准计划、反例、编译manifest与追踪矩阵 |
| [规划到复核场景](tests/scenarios/test_p2_planning_and_review.py) | mock链路、账本、复核版本绑定与单次修订的端到端检查 |
| [参考说明（数据版本）](https://github.com/kyrie21z/SWE-Olympus/blob/d40ba17cfe07c9d10bb818af8c7bcb27ac4d61d2/docs/reference-notes.md) | 课程结构与界面参考来源 |

数据版本链接固定到对应提交；其中的“待合并”描述属于当时的交付记录。P2交接记录与规划夹具随 `feat/p2-agents-planning` 分支提供，合入 `main` 后相对链接仍然有效。最新分支状态以本README的进度说明及GitHub为准。成员完成下一段后，应同步更新能力表、启动步骤、检查记录和文档入口。

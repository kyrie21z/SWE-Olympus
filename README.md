# SWE-Olympus

面向软件工程课程的多 Agent 工作流原型，由 **6 人、8 周**协作完成。项目承接上一阶段的需求规格文件，将业务需求逐步转成设计规格、开发任务、可视化编排与验收记录。业务内容由输入决定，校园报名仅是构造测试案例。

> **当前进度（2026-11-15）：** P1 工程、数据契约、存储和导入页面，以及 P2 Runner、规划、复核与静态编译已随 PR #14/#15/#17 进入 main。本次修订补齐开放业务输入、语义审查结果接纳、依赖版本与取消边界。在线模型质量、动态调度、真实执行及完整前端联调仍待后续验证或接入。

## 导航

[流程与进度](#workflow) · [技术与角色](#architecture) · [启动](#quick-start) · [文件输入](#file-input) · [目录](#structure) · [检查](#checks) · [六人协作](#collaboration) · [常见问题](#faq) · [文档](#references)

<a id="workflow"></a>

## 1. 流程与进度

```mermaid
flowchart LR
    REQ["需求文件 REQ"] --> IN["正式接纳"]
    IN --> DS["Athena：设计规格 DS"]
    DS --> PLAN["Daedalus：任务计划"]
    PLAN --> REVIEW["Momus 复核 / YOU 确认"]
    REVIEW --> RUN["Zeus 编排 / 受控执行"]
    RUN --> ACCEPT["独立复核 / 真人验收"]
```

图示为目标流程。模型生成内容，程序执行规则与状态检查，真人负责关键取舍与确认。

| 能力 | 本版本状态 |
| --- | --- |
| 工程、依赖锁文件、health、统一脚本、CI和路径工具 | 已有 |
| REQ/DS/PLAN等公共契约及同源Schema/TypeScript | 已有 |
| SQLite事务快照、事件追加、条件提交与幂等回执 | 已有，供受信服务使用 |
| 文件导入、REQ预检、只读展示、浏览器保存及原文导出 | 已有 |
| Agent Runner、输入白名单、输出审计、请求账本与执行记录 | 已有 |
| 任务卡、静态DAG编译、追踪矩阵、单次内容修订 | 已有 |
| 模型计划正文接入、语义审查发现进入独立报告 | 本次修订已接入，使用离线替身验证 |
| 非校园业务的规划→复核→编译 | 有构造走查，输出不套用校园模板 |
| RealProvider | 有配置与HTTP适配；替身传输检查通过不等于在线模型评测 |
| 正式REQ授权核验、Athena设计、动态调度、真实执行、恢复 | 待P3/P4/P5接入 |
| 页面调用后端规划、正式DAG/泳道/命令交互 | 待P5/P6接入 |

P2主要提供Python服务与无界面场景，当前网页仍是文件交接工作台。页面上的“生成设计规格”保持禁用，不能从是否有按钮判断后台模块是否存在。模型自评、退出码为零和UI颜色均不单独构成验收。

<a id="architecture"></a>

## 2. 技术与角色

| 层次 | 技术与用途 |
| --- | --- |
| 前端 | React / TypeScript / Vite / Ant Design / Lucide：文件交接和控制台 |
| 服务 | Python 3.12 / FastAPI：基础服务及后续API入口 |
| 工作流 | LangGraph：已有最小图；完整阶段图由P3实现 |
| 数据 | Pydantic / JSON Schema / AJV / SQLite：同源契约与事务记录 |
| 验证 | Ruff / pytest / Vitest / Playwright / GitHub Actions |

安装版本以[Python锁文件](requirements.lock)和[前端锁文件](frontend/package-lock.json)为准。

| 角色 | 类型与职责 |
| --- | --- |
| Athena | Agent：从有效REQ生成设计规格，保留来源与范围 |
| Daedalus | Agent：提出任务、依赖、输入输出及验收定义 |
| Momus | Agent：独立检查含义、遗漏、边界与范围外工作 |
| Hephaestus | Agent：实施授权变更，提交工件和执行记录 |
| Argus | Agent：独立复核工件和检查证据 |
| Hermes | 协作角色：集成与交付记录，不扩展为自动生产部署 |
| Zeus / Harness | 程序：状态、依赖、资源许可、预算、事件与命令接纳 |
| YOU | 真人：补充事实、处理取舍、批准阶段产物和最终验收 |

名字用于识别职责，输出采用普通工程文档和结构化数据。角色卡片不表示对应模型任务已经运行。

<a id="quick-start"></a>

## 3. 本地启动

需要 Git、仓库访问权限、**Python 3.12**、**Node.js 22.12+**及npm。P1/P2离线演示不需要模型密钥，首次安装依赖需要联网。

```text
git clone https://github.com/kyrie21z/SWE-Olympus.git
cd SWE-Olympus
```

Windows / PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock
npm ci --prefix frontend
python scripts/project.py check
```

macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
npm ci --prefix frontend
python3 scripts/project.py check
```

统一脚本优先使用当前仓库的虚拟环境，无需先激活。macOS/Linux没有python命令时使用python3。更新代码后，如锁文件有变化，重新执行安装命令。

分别在两个终端启动：

```text
python scripts/project.py backend
```

```text
python scripts/project.py frontend
```

- 前端：[http://127.0.0.1:5175](http://127.0.0.1:5175)
- 健康检查：[http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)
- 接口文档：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

health返回ok只表示基础服务正常，当前接口尚未包含完整规划/调度API。各终端使用Ctrl+C停止服务；端口被旧工作区占用时先停止旧服务，避免看到另一份代码。

### 模型配置

[.env.example](.env.example)中的OLYMPUS_MODE、OLYMPUS_DB属于后续接入约定，启动壳没有自动加载.env。离线演示无需该文件。

RealProvider需要通过环境变量或构造参数提供 `OLYMPUS_MODEL_API_KEY`、`OLYMPUS_MODEL_BASE_URL`、`OLYMPUS_MODEL_NAME`。BASE_URL应为模型API根路径，适配器追加/chat/completions。未配置时明确拒绝，不回退为模拟成功；仅配置变量不会让网页自动调用模型。凭据不得提交到仓库。

<a id="file-input"></a>

## 4. 文件输入与页面使用

操作顺序：**选择/拖入文件 → 查看预检 → 保存导入并查看 → 阅读条目/原文 → 导出或导入新版本**。可点击“下载交接样例（构造）”获取格式样例，再手动导入。

| 格式 | 行为 |
| --- | --- |
| REQ JSON | 检查契约、需求编号、正文指纹和确认目标，展示条目与来源 |
| Markdown / TXT | 按UTF-8原文展示，标为待整理材料 |
| PDF / Word / 其他 | 暂未接入解析，明确拒绝；不能仅改扩展名 |

单文件最多512 KB；浏览器最多保存20份、原文总计2 MB，不上传或跨设备同步。原文只读，名称和补充说明另存；更换文件建立新记录。旧Prompt草稿保留并可导出，不自动转换成REQ。损坏、配额不足和其他页面更新会明确报错。

REQ文件包含以下内容，完整格式见[样例](frontend/public/examples/requirement.json)和[公共契约](docs/contracts/README.md)：

| 字段 | 含义 |
| --- | --- |
| schema_version / kind | 契约1.0，对象类型REQ |
| object_id / version / content_hash | 需求标识、正整数版本和正文指纹 |
| content.requirements | 每条需求的id、text与acceptance |
| content.out_of_scope | 排除的范围 |
| provenance | 来源类别、标识及定位信息 |
| confirmation | 确认声明，可为空；非空时绑定目标版本/指纹与依赖 |

预检区分“校验未通过”“待整理材料”“待补充确认”“构造样例”“待服务端核验”。**本地保存不等于正式接纳**，文件中的human字段也不能证明身份或权限。正式核验由P4接入；修改正文需重新导出版本和确认，不在浏览器重签。

页面提供欢迎总览、项目中心、工作台、协作角色和使用指南。后续阶段使用空状态，当前没有假造生成或执行结果。

界面：[文件导入页](docs/preview/projects.png) · [工作台](docs/preview/workstation.png) · [手机布局](docs/preview/mobile.png)。截图是构造输入的交互演示。

<a id="structure"></a>

## 5. 目录与程序接入

```text
backend/olympus/
  app.py                   # health
  common/                  # 工作区和路径检查
  contracts/               # Pydantic公共模型、版本及指纹
  storage/                 # SQLite事务快照/事件/回执
  agents/runtime/          # Runner、provider、权限与执行记录
  agents/daedalus/         # 语义计划接入，程序生成版本/指纹
  agents/momus/planning/   # 程序检查、模型发现与有限返修
  reviews/common/         # 独立报告和问题关闭权限
  requests/               # 发送账本及query/cancel
  planning/               # 任务卡、校验、打包与静态编译
  traceability/planning/  # REQ→DS→Task追踪索引
frontend/
  src/imports/            # 文件读取、REQ预检、展示与本地保存
  src/contracts/          # 自动生成TS类型
  src/data/               # 客户端协议、mock、事件归并
  src/App.tsx             # 控制台，供P6扩展
  tests/data/             # 前端数据检查
  tests/e2e/              # 文件交接、恢复和手机布局
fixtures/                 # 公共对象、规划和模型响应的构造夹具
scripts/                  # 统一命令、契约/规划夹具生成
tests/                    # 单元、契约、存储、Runner、规划与场景
docs/                     # 契约、交接、参考和截图
.github/                  # CI及Issue/PR模板
```

- P2的 `DaedalusPlanner` 接收REQ/DS正文，模型返回任务语义字段；程序生成任务版本、指纹和精确输入绑定。完整Plan也按语义重新构造，不信任模型生成的批准或版本。
- 默认无provider适配器及内置Mock响应仅适用绑定版本的课程夹具；其他输入需要实际provider或显式匹配的测试响应。无效输出抛OutputSchemaError，不套用固定案例。
- `MomusPlanReviewer` 将模型发现与程序发现一并登记到CommonReviewer，阻断优先于通过声明；没有Runner时只标记heuristic-review，不伪称完成模型语义复核。
- `PlanService.prepare` 和编译器检查plan.requirement、plan.design、design.requirement的版本/指纹一致性。包始终保持approved=False、execution_started=False。
- `RequestLedger.cancel` 只有未发送或已获得提供方确认的请求才可确定取消；在途无确认保持outcome_unknown、预留预算和待核对标记。
- 正式状态只能由受信服务提交，MockClient命令仍返回NOT_READY；风险接受不等于验证通过，路径工具不等于完整执行隔离。

完整契约和后续接线见[P2交接说明](docs/p2-handoff.md)。运行中的请求结果未知时不能盲目重发；真实请求查询/取消证据由对应provider适配器提供。

<a id="checks"></a>

## 6. 检查与测试

| 命令 | 内容 |
| --- | --- |
| python scripts/project.py check | 契约/规划漂移、Ruff、后端测试、TS类型漂移、前端数据测试与构建 |
| python scripts/project.py test | 后端和前端数据测试 |
| python scripts/project.py scenario | 标记的无界面场景 |
| python scripts/project.py generate | 从公共模型和构造基线生成Schema、TS及夹具 |
| python scripts/project.py browser | 文件导入、刷新恢复、旧草稿及手机布局 |

首次浏览器检查：

```text
cd frontend
npx playwright install chromium
cd ..
python scripts/project.py browser
```

本机已有Chrome也可使用：

```powershell
$env:PLAYWRIGHT_CHANNEL='chrome'
$env:OLYMPUS_TEST_PORT='5176'
python scripts/project.py browser
```

测试端口独立于正在演示的5175，避免误用另一工作区的页面。CI使用Chromium和同一套统一命令。

本次新增回归位于[交接回归检查](tests/planning/test_handoff_regressions.py)，覆盖无效输出、另一业务、审查阻断、依赖失效、计划范围与取消，以及真实HTTP适配器的替身传输。既有校园八任务十二边与单次返修仍保留。具体数量及结果以[最新Actions](https://github.com/kyrie21z/SWE-Olympus/actions)和PR为准。

这些检查验证程序行为、构造样例和离线接线，不代表在线模型质量、真实执行或整个系统已经通过验收。

<a id="collaboration"></a>

## 7. 六人协作与交接

每人连续完成自己的两个Issue，并提交实现、必要测试、样例和说明。工作量是相对估计，不是工时承诺。

| 成员 | Issue | 交付 | 初估 | 下一位评审 |
| --- | --- | --- | --- | --- |
| P1 | [#1](https://github.com/kyrie21z/SWE-Olympus/issues/1)、[#2](https://github.com/kyrie21z/SWE-Olympus/issues/2) | 工程、契约、存储和前端数据基础 | 10 SP | P2 |
| P2 | [#3](https://github.com/kyrie21z/SWE-Olympus/issues/3)、[#4](https://github.com/kyrie21z/SWE-Olympus/issues/4) | Runner、复核、规划、追踪和静态编译 | 11 SP | P3 |
| P3 | [#5](https://github.com/kyrie21z/SWE-Olympus/issues/5)、[#6](https://github.com/kyrie21z/SWE-Olympus/issues/6) | 动态调度、资源许可和人工关口 | 10 SP | P4 |
| P4 | [#7](https://github.com/kyrie21z/SWE-Olympus/issues/7)、[#8](https://github.com/kyrie21z/SWE-Olympus/issues/8) | 正式REQ接纳、Athena、执行和工件验收 | 11 SP | P5 |
| P5 | [#9](https://github.com/kyrie21z/SWE-Olympus/issues/9)、[#10](https://github.com/kyrie21z/SWE-Olympus/issues/10) | 有界恢复、命令、HTTP和事件补取 | 11 SP | P6 |
| P6 | [#11](https://github.com/kyrie21z/SWE-Olympus/issues/11)、[#12](https://github.com/kyrie21z/SWE-Olympus/issues/12) | 四视图、统一命令面板和浏览器联调 | 10 SP | P1 |

从包含依赖的最新main建立分支；使用[PR模板](.github/pull_request_template.md)关联Issue，写清接口、验证级别和限制，经非作者评审再合并。堆叠PR在基础PR合入main后应调整目标分支并复核。Issue关闭和CI通过不单独替代业务验收；缺陷回原负责人修复，不统一留给P6。

<a id="faq"></a>

## 8. 常见问题

| 问题 | 处理 |
| --- | --- |
| 为什么没有样例按钮或刷新后记录消失？ | 核对是否从最新main安装启动，端口是否仍被旧目录的Vite占用；不要清除未备份的浏览器数据。 |
| 为什么不能点击生成设计？ | 当前页面尚未接入正式接纳与生成服务，需P4/P5/P6接线。 |
| 只有一段Prompt怎么办？ | 先在上游整理确认，也可作为MD/TXT预览；不会自动获得确认效力。 |
| 为什么改了JSON后失败？ | 正文指纹或确认目标可能失配，应从上游导出新版本。 |
| 没配模型能规划任意项目吗？ | 内置mock仅绑定课程夹具。其他业务需要provider或显式测试响应，不能静默套模板。 |
| 5175端口占用怎么办？ | 停止旧服务，或用npm run dev --prefix frontend -- --port 5176临时换端口。 |
| 缺少antd或Python包怎么办？ | 在当前目录重新按锁文件安装依赖。 |
| 浏览器检查找不到浏览器？ | 安装Playwright Chromium，或使用已安装的Chrome通道。 |
| 构建提示主包较大？ | 是后续按页面拆分加载的优化项，以退出码判断是否构建失败。 |

<a id="references"></a>

## 9. 文档

[文件交接](docs/input-handoff.md) · [公共契约](docs/contracts/README.md) · [P1交接](docs/p1-handoff.md) · [P2交接](docs/p2-handoff.md) · [参考说明](docs/reference-notes.md) · [规划夹具](fixtures/plans/) · [Issue](https://github.com/kyrie21z/SWE-Olympus/issues) · [PR](https://github.com/kyrie21z/SWE-Olympus/pulls)

后续成员接棒时同步更新能力表、启动步骤、检查证据与文档入口；区分程序已实现、构造走查通过和真实模型已验证。
# P1交付与P2接棒

日期：2026-11-15。对应Issue #1、#2，拆为PR #14、#15；工程基础先合入main，数据增量随后经PR #17进入main。两项Issue已关闭，以下记录P1交付范围；P2及本次回归修订见[p2-handoff](p2-handoff.md)。

## 本段交付

| Issue | 实现 | 验收入口 |
| --- | --- | --- |
| #1 | Python/LangGraph与React/TS启动壳、health、锁文件、统一脚本、CI、PR模板、临时工作区与路径边界；文件导入页面壳 | tests/unit、tests/scenarios、浏览器基础检查 |
| #2 | 公共对象、规范指纹、同源JSON Schema/TS、合法非法样例、八任务十二边、SQLite原子状态/事件/回执、前端协议/mock/归并；REQ本地预检及导入存储 | tests/contracts、tests/storage、frontend/tests/data |

界面保留浅色控制台、项目列表与标签结构，入口改为文件交接。主线为 **REQ导入 → Athena设计规格 → Daedalus任务分解与审查确认 → Zeus编排执行 → 验收**。开放的是输入文件的业务领域；不再重复上游的想法讨论。P6继续负责正式DAG、Agent泳道、追踪矩阵及命令交互。

## 验证入口

- `python scripts/project.py check`：契约漂移、Ruff、36项后端测试、18项前端数据测试、TypeScript与Vite构建，本地通过。
- `python scripts/project.py browser`：4项检查在本机Chrome全部通过，覆盖文件预检/只读展示/导出、结构化REQ刷新恢复、旧草稿保留、手机导航与宽度。远程结果以对应提交的CI为准。
- 浏览器截图存放于 `docs/preview/`；REQ样例为人工构造，明确标记synthetic_fixture。
- 当前检查覆盖P1基础服务、离线数据与导入交互，不证明真实Agent、生成服务或调度已运行。

本机使用Chrome时设置 `PLAYWRIGHT_CHANNEL=chrome`，CI使用安装的Chromium。本机Edge曾在创建浏览器上下文时异常关闭，因此改用Chrome完成整套检查。依赖弃用提示及前端包体积提示需在后续升级、性能优化时处理。

## 后续接线

1. P2导入 `olympus.contracts.models`，消费固定DS/PLAN夹具；H-01为构造记录。规划器通过Python `fingerprint`绑定任务语义，新增Runner、独立复核器及静态manifest。
2. P3在提交前完成权限、状态、预算、证据及资源校验，再使用Store事务；阶段图与interrupt关口由P3实现。
3. P4接收原始REQ文件并在服务端重新校验契约、来源、确认人权限和依赖。通过后生成真实Design，保留精确REQ引用。不能把前端 `canSave` 当作正式接纳。
4. P5适配HTTP、恢复及补拉；P6将真实产物、命令回执和事件接入当前页面。不要用测试夹具填充生成结果。
5. 契约改动先改Python，运行generate，提交生成差异、兼容影响与对应场景。

## 文件交接边界

`frontend/src/imports/model.ts`保存文件名、格式、原文、补充说明及导入时间；`inspect.ts`输出每次重新计算的本地预检结果，`storage.ts`独立保存浏览器记录，不写SQLite正式运行状态。

Issue #1只提供会话内预览与JSON语法检查；Issue #2增加公共REQ契约预检、本地持久化和格式样例。MD/TXT保持待整理材料，PDF/Word明确拒绝；原文只读，不在前端重新签署确认。详细格式、状态和接入要求见[文件交接说明](input-handoff.md)。

## 关键边界

- Store是受信服务层原语，不向浏览器开放commit；它不代替业务审批。
- MockClient.submit稳定拒绝NOT_READY；点击页面不能乐观标记任务accepted。
- 事件归并处理重复与缺号，HTTP传输及调度handler由后段接入。
- LangGraph仅验证框架导入与最小状态图；未实现完整业务流程。
- 工作区路径校验不等于生产隔离；P4负责受控工具、执行隔离及最终产物复核。
- 旧Prompt存储保留并支持原样导出；不自动转成已确认需求。

后续成员直接从最新main接棒；已合入主分支的公共类型、存储和导入页面无需再检出历史分支。代码缺陷由对应作者修复。

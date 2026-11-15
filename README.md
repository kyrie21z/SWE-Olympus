# SWE-Olympus

从确认需求到任务编排的多Agent课程项目。当前分支交付 **P1 / Issue #1、#2**：工程、公共契约、SQLite事件/状态存储、前端数据基础及工作台预览。

## 快速开始

需要 Python 3.12、Node.js 22.12+。无需模型密钥。先在仓库根目录：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock
npm ci --prefix frontend
python scripts/project.py check
```

macOS/Linux将安装命令中的`.venv\Scripts\python`换成`.venv/bin/python`。统一入口会自动使用本项目虚拟环境；CI则使用已安装依赖的Python。

分别打开两个终端：

```text
python scripts/project.py backend
python scripts/project.py frontend
```

打开 `http://127.0.0.1:5173`；后端健康检查 `http://127.0.0.1:8000/health`。前端保留 Ant Design Pro 风格，以**导入上一阶段的需求规格文件**为主入口：REQ JSON 支持结构预检及条目预览，Markdown/TXT 作为待整理材料保存。文件业务领域不限，支持本地保存、搜索、刷新恢复和原文件导出。**当前是文件交接与流程预览；正式确认核验、设计生成、任务分解和执行仍待后续模块接入。** PDF/Word 解析尚未接入，不会把上传成功显示为需求已确认。

## 统一命令

| 命令 | 内容 |
| --- | --- |
| `python scripts/project.py check` | 契约漂移、Python静态检查/全部测试、TS类型漂移、前端数据测试/构建 |
| `python scripts/project.py test` | 后端测试与前端数据测试 |
| `python scripts/project.py scenario` | 无界面场景，后续模块可追加 |
| `python scripts/project.py generate` | 从Python模型生成JSON Schema、夹具与TS类型 |
| `python scripts/project.py browser` | 浏览器基础检查；首次在frontend运行`npx playwright install chromium` |

CI执行相同check与browser命令。测试失败返回非零退出码；分支保护是否开启由仓库管理员配置。

## 目录与接棒

```text
backend/olympus/
  app.py                 # health入口
  contracts/             # Pydantic唯一类型源
  storage/               # SQLite事务、事件、CAS和幂等回执
  common/workspace.py    # 路径范围与临时工作区
frontend/src/
  contracts/             # 自动生成的TS类型
  data/core/             # 客户端协议、事件归并/缺口检查
  data/mock/             # 离线适配器
  imports/               # 文件读取、REQ预检、只读预览与本地存储
  App.tsx                # Ant Design控制台，供P6扩展
  main.tsx               # 主题与应用入口
fixtures/contracts/      # 合法/非法样例、8任务12边、7类状态
tests/                   # 单元、契约、存储和场景检查
docs/                    # 契约、参考和P2交接
```

P2消费公共模型、固定DS与PLAN夹具，增加Runner/复核器和静态规划。P3实现动态调度和命令关口；P4接正式REQ导入、设计与受控执行；P5恢复/HTTP；P6扩展完整可视化。正式运行共享状态只能由服务端受信提交，运行缓存只读；MockClient命令明确返回NOT_READY。本地导入记录与正式REQ/DS/Run对象分离，文件自带的确认记录不能代替服务端授权校验。

详细语义见[文件交接说明](docs/input-handoff.md)、[公共契约](docs/contracts/README.md)、[P1验收与交接](docs/p1-handoff.md)、[参考说明](docs/reference-notes.md)。页面提供[REQ格式样例](frontend/public/examples/requirement.json)，明确标为构造数据；既有校园案例仅保留在自动测试夹具中。两者均不作为产品默认输入或生成结果，synthetic_fixture 不能冒充真人决定。

## P1分为两个PR

先合并`feat/p1-01-foundation`（Issue #1工程基础），再合并`feat/p1-02-contracts`（Issue #2公共契约、存储与数据模块）。第二个PR当前以第一个分支为base，仅显示增量；基础PR合并后切到main并复核。原合并版PR #13由这两个PR替代。

## 界面与数据边界

- [文件导入页](docs/preview/projects.png) / [规格交接工作台](docs/preview/workstation.png) / [手机版](docs/preview/mobile.png)。截图使用构造材料演示交互，不代表生成或执行结果。
- 文件只保存在当前浏览器的当前站点，不上传、不跨设备同步。单文件最多512 KB，最多20份，总原文不超过2 MB；请保留原文件备份。
- 格式异常、版本不兼容、存储拒绝或空间不足会明确报错；不会以空列表覆盖损坏的原数据。
- 保存前检查其他页面是否已更新。该检查只是本地冲突提示，localStorage不提供跨标签页事务；正式多用户并发由后续HTTP/数据库服务接管。
- 旧版Prompt草稿保留在原存储项中，可导出，不会自动转换为REQ或删除。导入原文只读，补充说明另存，更换文件建立新记录。
- 样例状态与风险验收边界继续由数据层测试覆盖，未生成产物使用空状态；生成设计规格按钮保持禁用。

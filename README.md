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

打开 `http://127.0.0.1:5173`；后端健康检查 `http://127.0.0.1:8000/health`。界面包含项目入口、工作台、角色目录及公共契约说明，工作台可切换七类构造场景。**页面数据均为mock；真实设计生成、调度、模型执行与批准尚待后续阶段接入。**

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
  main.tsx               # 基础壳，供P6扩展
fixtures/contracts/      # 合法/非法样例、8任务12边、7类状态
tests/                   # 单元、契约、存储和场景检查
docs/                    # 契约、参考和P2交接
```

P2消费公共模型、固定DS与PLAN夹具，增加Runner/复核器和静态规划。P3实现动态调度和命令关口；P4接设计与受控执行；P5恢复/HTTP；P6扩展完整可视化。P1不提前实现这些业务层。共享状态只能由服务端受信提交，前端缓存只读；MockClient命令明确返回NOT_READY。

详细语义见[公共契约](docs/contracts/README.md)、[P1验收与交接](docs/p1-handoff.md)、[参考说明](docs/reference-notes.md)。构造案例采用校园自动候补，H-01为synthetic_fixture，不能冒充真人决定。

## P1分为两个PR

先合并`feat/p1-01-foundation`（Issue #1工程基础），再合并`feat/p1-02-contracts`（Issue #2公共契约、存储与数据模块）。第二个PR当前以第一个分支为base，仅显示增量；基础PR合并后切到main并复核。原合并版PR #13由这两个PR替代。

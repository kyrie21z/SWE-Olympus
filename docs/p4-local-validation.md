# P4 本地实现与验证记录

日期：2026-11-15。基于 main `8af3329ec95f27b20fb75502ae978f2d54a01c79`。

## 本地状态

- 分支：`feat/p4-design-execution`。
- OL-07 / OL-08 的实现、测试、夹具、演示和交接说明已写入本地。
- 没有本地提交、远端推送、创建合并申请、修改远端任务或写入模型密钥。
- 本机 Git 凭据助手在受限进程中无法启动，因此通过已连接的 GitHub 只读获取源码。
  文件对象、目录树对象和原始签名提交记录均按 Git 指纹校验后，建立本地浅历史仓库。
  当前提交是原始主分支提交，不是另造一个无关的初始提交；`git fsck --no-reflogs` 通过。

## 实际通过的检查

| 检查 | 结果 |
| --- | --- |
| `python scripts/export_contracts.py --check` | 通过，公共契约与生成物未漂移 |
| `python scripts/export_plans.py --check` | 通过，原有规划夹具未漂移 |
| `python -m ruff check backend tests scripts` | 通过 |
| P4 组件与无界面场景 | 64 项通过，1 项跳过 |
| 排除缺失 LangGraph 的关口目录和 foundation 文件后的回归 | 292 项通过，2 项跳过 |
| `python -m compileall -q backend scripts` | 通过 |
| `git diff --check` | 通过 |
| `python scripts/p4_demo.py` | 实际完成 T01/T03：2/8，生成真实工件和检查输出 |

组件检查命令：

```powershell
.\.venv\Scripts\python.exe scripts/p4_verify.py --components-only
```

回归检查实际命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  --ignore=tests/scenarios/gates `
  --ignore=tests/scenarios/test_foundation.py `
  --basetemp=.olympus/pytest-review-final -o cache_dir=.olympus/pytest-cache
```

运行时设置了 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`、`PYTHONUTF8=1`，并将 TEMP/TMP
指向本仓库 `.olympus/pip-temp`，避免宿主受限临时目录权限问题。

## 本轮复核修正

- 统一调度器与执行器的锁顺序，并对同一服务内的设计生成、验收收集增加并发保护。
- 回执、操作别名和模型请求绑定运行编号，避免共用证据目录时跨运行复用。
- 设计旧版本只显示自己的报告；返回对象不再与内部设计记录共享可变内容。
- 独立验收报告随回执保存，重复收集和恢复后的查询可复用有效报告；未知复核请求不会盲目重发。
- 补充任务状态、完整验收报告及复核请求的查询投影，供 P5、P6 接入。
- 实际工具进程次数如实记录；证据落盘失败会结束为失败，不再让等待方一直阻塞。
- 检查报告明确区分后端检查与完整项目检查。README 的阶段图和目录说明与本地实现同步。

上述修正增加了 10 项反例，已计入本页的 64／292 项通过结果。最新演示证据位于
`.olympus/p4-demo-d93147f5/report.json`，依然只表示 T01、T03 两项业务任务的演示。

## 环境限制与未验证项

锁文件安装在本机网络代理下遇到 PyPI TLS 连接失败，没有改动 `requirements.lock`。
为完成可执行验证，只在本项目 `.venv` 中复用了已有本地测试工具；未安装/修改全局包。

| 项目 | 本次实际环境 | 项目锁定要求 |
| --- | --- | --- |
| Python | 3.12.5 | 3.12 系列 |
| pytest | 8.4.2 | 8.4.2 |
| Pydantic | 2.13.4 | 2.12.5 |
| Ruff | 0.12.0 | 0.14.5 |
| LangGraph | 未安装 | 1.0.3 |

因此本地结果**不等于锁定依赖环境下的完整自动检查通过**：

- `tests/scenarios/gates` 的原有八项 LangGraph 场景和 foundation 场景未运行。
- P4 新增的真实 LangGraph 设计关口接线测试明确跳过；命令总线和业务审批校验实际运行。
- 路径工具的符号链接用例因宿主不允许创建链接而跳过；其余普通路径和越界用例通过。
- 没有改动前端代码或公共数据结构，本轮没有安装前端依赖并重跑浏览器/构建检查。
- 没有调用在线真实模型，模型结论来自明确标记的模拟响应；Python 进程、文件、检查日志是真实的。
- 操作系统级恶意代码隔离、完整跨进程恢复、HTTP 和四视图仍不属于本段验证结论。

## P5 评审前应补跑

在能安装锁定依赖的环境中运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
npm.cmd ci --prefix frontend
.\.venv\Scripts\python.exe scripts/p4_verify.py
.\.venv\Scripts\python.exe scripts/project.py check
.\.venv\Scripts\python.exe scripts/project.py browser
```

`.olympus/p4-verification.json` 是可重现的组件报告；`.olympus/p4-demo-*/report.json`
和对应执行记录目录包含实际工件、脚本、检查输出和输入指纹，不进入版本库。
完整合并申请应由 P5 非作者复核，分别核对 #7、#8，并以实际待合并提交的自动检查为准。

# P1交付与P2接棒

日期：2026-11-15。对应Issue #1、#2，分支 `feat/p1-foundation`。代码与本地验证已准备，非作者评审和远程CI以PR记录为准；未合并前不关闭Issue。

## 本段交付

| Issue | 实现 | 验收入口 |
| --- | --- | --- |
| #1 | Python/LangGraph与React/TS启动壳；health；锁文件；统一脚本；CI与PR模板；临时工作区及路径边界 | tests/unit、tests/scenarios、浏览器基础检查 |
| #2 | 公共对象、规范指纹、同源JSON Schema/TS、合法非法样例、八任务十二边；SQLite原子状态/事件/回执；前端协议/mock/归并 | tests/contracts、tests/storage、frontend/tests/data |

为了回应课堂参考图，额外完成轻量项目入口与工作台壳：侧边导航、任务选择、详情/依赖/事件标签，以及七类构造状态切换。P6仍负责正式DAG画布、Agent泳道、追踪矩阵与人工命令交互。角色目录只说明职责，没有声称模型已运行。

## 本地验证

- `python scripts/project.py check`：契约漂移检查、Ruff、后端35项测试、前端6项数据测试、TypeScript与Vite构建。
- 浏览器使用本机Edge（Playwright Chromium通道），2项检查：工作台场景/风险状态切换、390px手机布局。
- 桌面1440px及手机截图已查看；没有横向溢出，切换风险场景不增加验收计数。
- 路径检查包含Windows junction越界；测试创建的临时工作区会自动清理。
- 测试只覆盖P1基础服务与mock数据，不证明真实Agent、完整业务调度或自动生成代码已跑通。

本地Edge运行方式（未下载Playwright自带Chromium时）：

```powershell
$env:PLAYWRIGHT_CHANNEL='msedge'
python scripts/project.py browser
```

CI使用Playwright安装的Chromium。Python依赖有两条上游弃用提示（Starlette BlockingPortal及LangGraph序列化默认值），当前测试通过；升级相关依赖时复核。

## P2拿到什么

1. 通过README安装并运行统一check；从合并后的main开始下段分支。
2. 导入 `olympus.contracts.models`；fixture中的REQ/DS/PLAN来自人工构造，H-01不能作为真人确认。
3. 规划器用固定DS构造Task/Plan；调用`fingerprint`生成规范指纹。Plan任务清单绑定每个Task的版本/指纹。
4. 请求账本/独立复核器/静态manifest仍由P2实现；现成Store可提供事务持久化方式，尚不提供请求provider查询。
5. 新增场景放入`tests/scenarios`，契约变更先改Python并运行generate；提交生成差异与兼容影响。

## 关键边界

- Store是受信服务层的存储原语。P3必须在提交前完成权限、状态、预算与证据业务校验；禁止从浏览器直接调用commit。
- MockClient的submit稳定拒绝NOT_READY；不得在页面点击后乐观标记任务accepted。
- 无界面事件归并已处理重复与缺号，HTTP传输、断线拉取循环与调度handler由后段接入。
- 本次LangGraph只验证框架导入及最小状态图，正式阶段图与interrupt确认关口属于P3。
- 工作区路径校验不是生产安全沙箱；P4继续负责执行隔离策略、注册工具白名单及产物复核。
- 文档content中的业务结构是课程原型粒度；复杂接口参数/实体定义可在P2/P4扩展并提升schema版本。

主PR关联两条Issue，由P2独立评审。代码缺陷由对应作者修复，不将后端验收留给P6。

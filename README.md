# SWE-Olympus · P1工程基础

本PR只交付Issue #1：Python/FastAPI/LangGraph与React/TypeScript/Vite启动壳、统一检查入口、CI、路径/临时工作区与协作模板。

## 启动与检查

需要Python 3.12、Node.js 22.12+，无需模型密钥。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock
npm ci --prefix frontend
python scripts/project.py check
```

Linux/macOS将安装命令的Python路径换成`.venv/bin/python`。

分别在两个终端运行：

```text
python scripts/project.py backend
python scripts/project.py frontend
```

页面：http://127.0.0.1:5173；health：http://127.0.0.1:8000/health。
前端参考 Ant Design Pro，采用 Ant Design 6 的浅色控制台：欢迎总览、开放式需求表单、项目列表、工作台、角色说明与使用指南。支持任意业务 Prompt 的创建、编辑、搜索和 JSON 导出；本 PR 草稿只保留在页面会话，刷新后清除。Issue #2 接入浏览器本地持久化。生成服务尚未接入，页面不展示虚构任务或默认校园项目。

`python scripts/project.py check`执行Ruff、后端全部测试和TypeScript/Vite构建。
`python scripts/project.py scenario`执行无界面场景。
首次在frontend运行`npx playwright install chromium`，然后`python scripts/project.py browser`执行基础页面检查。
本机已有Edge时可设`PLAYWRIGHT_CHANNEL=msedge`。CI使用Chromium。

## 边界与交接

`backend/olympus/common/workspace.py`提供相对路径/越界/父子重叠检查与临时工作区；不宣称生产沙箱。
`tests/conftest.py`提供workspace夹具，各阶段追加自身检查。
本PR的前端依赖已锁定为P1共用版本，Issue #2无需更改安装方式。

**合并顺序：工程基础PR（#1）→数据与契约PR（#2）。** 第二个PR基于本分支，先评审第一层，再接入第二层。
P1两个Issue分别关闭；原先“一主PR关联两项”的约定在本次交付中替换为两个PR。

界面参考：[Ant Design Pro](https://preview.pro.ant.design/welcome/)。保留现有 React/Vite 工程，使用 Ant Design 组件；未迁移 Umi 或复制参考站品牌。

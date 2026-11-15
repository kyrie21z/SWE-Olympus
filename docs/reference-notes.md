# 本次参考与采用方式

用户提供的课堂图片用于提取“左侧导航 → 项目入口 → 工作台内任务列表与详情”的信息结构。最新界面参考用户指定的[Ant Design Pro](https://preview.pro.ant.design/welcome/)，使用 Ant Design 6.7.0 实现白色侧栏、蓝色主色、卡片与表单层次，保留 React/Vite 工程；未复制ASF品牌或将其全部菜单承诺为本项目功能。

读取用户本地 `D:/吴恩达agent` 中的三篇学习笔记：

- `1.6 任务分解-识别工作流程步骤`：采用显式输入输出、独立工作包和模块化接口。
- `4.4 组件级评估`：将契约、存储、路径与前端数据归并各自测试，后续成员维护各自场景。
- `5.7 多智能体系统的通信模式`：采用共享状态与集中流程控制；具体角色执行与审批由后续模块接入，不引入任意自由对话路由。

上述内容用于设计参考，没有复制课程文件、图片或外部源码入仓库。

实现时核对的官方资料：[LangGraph概览](https://docs.langchain.com/oss/python/langgraph/overview)、[Pydantic JSON Schema](https://docs.pydantic.dev/latest/concepts/json_schema/)、[Vite入门](https://vite.dev/guide/)。项目锁定具体版本，未以“最新版本”作兼容承诺。

本轮核对官方[Layout](https://ant.design/components/layout)与[Form](https://ant.design/components/form)文档；通过实际参考页观察布局。没有复制参考站源码或插图，装饰图由本项目布局与已有图标组成。

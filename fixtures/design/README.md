# P4 设计测试材料说明

统一的构造需求和设计仍来自 `fixtures/contracts/valid.json`，不另建第二套事实来源，也不会把其他业务输入静默替换为校园模板。

`tests/design/test_design_pipeline.py` 显式提供与该测试需求绑定的 Athena／Momus 响应，覆盖：

- 增加短信等范围外功能；
- 遗漏取消规则；
- 遗漏状态或并发设计；
- 使用过期依赖；
- 模型输出格式不合格；
- 独立语义审查提出阻断问题。

必须显式使用 `RequirementBridge(allow_synthetic=True)` 才能接纳构造样例。该选项不会让 `YOU-fixture` 获得真实人工确认权限。真实导入需要服务端提供授权人员、受信来源编号以及匹配的上游确认记录。

运行 `python scripts/p4_demo.py` 可以查看无界面串联演示。输出包含模型模式、来源、构造确认、任务与工件指纹，以及实际工具检查结果。

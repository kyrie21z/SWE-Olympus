"""编排层：调度（`scheduler/`）与状态机（`state_machine/`）。

对应 issue #5 的交付边界：
- `scheduler/`：运行态三集合、稳定排序、原子派发、资源/人员互斥、执行替身
- `state_machine/`：Run 快照、受控迁移、事件
静态拓扑、优先级索引与冲突索引来自 P2（`planning.compiler`），本层不重算。
"""

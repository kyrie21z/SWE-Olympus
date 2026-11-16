# P6 演示脚本

1. 启动 P5 真实 API 的本地契约运行：

   ```powershell
   .\.venv\Scripts\python.exe scripts\p6_demo_server.py
   ```

   该脚本创建临时 SQLite，绑定真实 PLAN 和追踪文档，不使用前端 mock。
2. 启动前端：

   ```powershell
   cd frontend
   npm run dev
   ```

3. 打开 `http://127.0.0.1:5175/?run=RUN-campus#console`。
4. 指出 accepted n/N、阻塞原因和阶段都来自 `/view`。
5. 选中 T03，查看任务详情与 execution API 返回的检查证据。
6. 打开“人工命令”，说明禁用动作来自 `allowed_actions.reasons`。
7. 提交一个允许的命令，展示 Receipt、durability 和 recorded event 数。
8. 再次提交同一 operation，展示相同回执，不重复乐观更新。
9. 打开追踪表，展示“已验证”和“未验证 · 人工接受”的区别。
10. 缩窄到 390px，展示 DAG、表格内部滚动和页面无横向溢出。

若当前演示库没有可查询 execution，页面会显示
`EXECUTION_VIEW_UNAVAILABLE`。这是诚实状态，演示时不能改成假工件。

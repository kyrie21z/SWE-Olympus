# 公共契约 1.0

源文件为 `backend/olympus/contracts/models.py`。`python scripts/project.py generate` 生成JSON Schema、构造夹具及TypeScript；检查入口验证产物未漂移。不得手改生成文件。

## 对象和版本

| 对象 | 主要字段 / 规则 |
| --- | --- |
| Requirement | object_id是requirement_id；content含requirements/验收及out_of_scope；confirmation绑定Ref |
| Design | object_id是design_id；requirement为精确Ref；content含模块、数据、接口、状态、并发、异常、交互、ADR及来源元素 |
| Plan / Task | Plan绑定REQ、DS及任务版本/指纹清单；Task含来源、输入输出、前驱、执行者/复核者、写入范围、检查与预算策略 |
| Run / TaskRuntime | mode、批准包、状态修订号、事件游标、预算；任务状态与验证状态、人工处置分开 |
| Attempt | logical_task_id / attempt_id / provider_request_id / idempotency_key独立；未知结果是outcome_unknown |
| Artifact / Review | 内容指纹、输入/实现/测试/夹具版本；独立复核身份、结论与证据 |
| Event | run内单调event_seq、旧/新状态、触发者、时间、state_revision及同事务快照 |
| Command / Receipt | operation_id、actor、动作、目标、expected_version/state_revision；接纳、拒绝或等待及原因 |
| ExecutionRecord | 输入、工具返回、diff、工件、检查、耗时、调用和重试；不保存模型内部推理 |

各对象 `schema_version="1.0"`，未知字段拒绝；对象版本为正整数。`object_id/version/content_hash`组成Ref。`Task`指纹覆盖任务语义字段；REQ/DS/PLAN仅规范化显式content字段。布局、运行耗时和签名均位于该内容域外；禁止在content中混入它们。JSON规范化为UTF-8、键排序、紧凑分隔符、拒绝NaN，SHA-256。Python为指纹唯一生成端；跨语言不能自行假设浮点序列化完全相同。

内容变化需生成新版本并更新指纹；依赖变化需更新对应Ref并失效相关确认。P1校验引用结构/本对象指纹/REQ确认目标，P2/P3负责跨对象引用存在性、依赖一致性、权限和确认提交的业务关口。构造H-01的origin是synthetic_fixture，不能充当真人授权。

## 存储提交边界

`Store.create(run, version=1)`幂等创建初始快照。`Store.commit(command, proposed, reason=...)`由受信服务调用：先按operation_id核对请求与效果指纹，再在BEGIN IMMEDIATE事务内检查版本/修订号；一次提交更新快照、追加事件并保存回执。失败整体回滚。旧版本保存rejected回执；同操作同输入重放返回相同结果，同ID异内容抛IdempotencyConflict。run版本变化通过next_version显式递增。

proposed携带提交前的当前游标，由存储分配新序号和修订号。事件可按after_seq补取，snapshot一次读取对应完整状态；不要把分开读取的“最新快照”和旧游标拼成一个视图。事件自带同事务快照，可从seq=0初始状态重放。SQLite触发器拒绝事件更新/删除，数据库管理员仍有修改能力。

P1的commit是**受信持久化接口**，不对浏览器暴露。它不代替P3的身份校验、转移许可、accepted证据复核或原子资源分配。事件快照为课程规模的简化，后续数据增长可演进为增量事件。

## 前端缓存

`OlympusClient`统一getSnapshot/getEvents/submit。MockClient返回拷贝，命令返回NOT_READY，不更改快照。P5/P6只需适配HTTP传输。

`mergeEvents`校验版本和同一run，排序去重，拒绝相同序号不同内容；遇到缺号或修订号断裂停在最近连续快照，返回needsRefetch与afterSeq。只应用服务端提交的snapshot，不用new_state自行推断成功。AJV校验结构，补充风险/验收等跨字段检查；Python语义验证仍是权威。

风险接受以disposition和理由保存，state不会变成accepted，verification_status不变成passed。未知结果仍为运行中，关联Attempt说明outcome_unknown；过期任务verification_status=stale。前端完成数只计accepted且passed。

## 本地工作区

`resolve_scoped`拒绝绝对路径、盘符、目录穿越、数据流和解析后越界的链接；`scopes_overlap`检测相同/父子路径。调用方应紧接文件操作前复查；此工具不能防御恶意进程在检查与写入之间更换链接，也不是生产级沙箱。P4仍须使用受控适配器与最终产物检查。

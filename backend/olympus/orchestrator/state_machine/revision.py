"""改版失效（issue #5："取消/**改版**与验收竞争只允许合法迁移"）。

上一步只做了"取消"这一半。改版是另一半，语义完全不同：

| 场景 | 谁是终态 | 怎么处理 |
| --- | --- | --- |
| 取消 | `cancelled` 不可逆 | 拒绝后续迁移 |
| 改版 | `accepted` 对**新版本**不再适用 | 回退到 `waiting`，验收标 `stale`，历史证据保留 |

契约原文（`TaskRuntime.evidence_status` 的注释）：

> 已验收记录不可被新版本覆盖。变更后将旧验收标注为"对当前计划不再适用"，
> 当前任务回 waiting/blocked；历史 accepted 与原证据保留。

所以改版**不是**"删掉记录"，而是"状态后退 + 验收标记过期"。`Verification` 里的
`stale` 值就是为这件事准备的。

顺序很要紧：`accepted` 要求 `verification_status == "passed"`，所以必须
**先离开 accepted，再标 stale**，反过来会触发 pydantic 校验失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from olympus.contracts.models import Plan

from .run_context import RunContext


@dataclass
class RevisionOutcome:
    """一次改版的影响面。"""

    roots: list[str] = field(default_factory=list)        # 直接受影响的任务
    invalidated: list[str] = field(default_factory=list)  # 实际被回退的任务（含后继）
    kept: list[str] = field(default_factory=list)         # 仍适用、未动

    def to_dict(self) -> dict:
        return {"roots": self.roots, "invalidated": self.invalidated, "kept": self.kept}


def affected_closure(manifest, roots: list[str]) -> list[str]:
    """沿 manifest 的后继闭包计算影响面。

    issue 明确"不重算静态拓扑"——后继关系已经在 `CompiledManifest.descendants` 里，
    这里只做集合运算，不重新求一次依赖图。
    """
    out: list[str] = []
    for root in roots:
        if root not in out:
            out.append(root)
        for item in manifest.descendants.get(root, []):
            if item not in out:
                out.append(item)
    return out


def invalidate_revision(
    ctx: RunContext,
    plan: Plan,
    manifest,
    roots: list[str],
    *,
    reason: str,
    actor: str = "YOU",
) -> RevisionOutcome:
    """把受影响任务（含后继）回退：accepted → waiting，验收标 stale，代际 +1。

    只动"已经验收过"的任务。还在 waiting/ready 的不需要回退——它们本来就没通过。
    """
    outcome = RevisionOutcome(roots=list(roots))
    targets = affected_closure(manifest, roots)

    for task_id in targets:
        runtime = ctx.runtime(task_id)
        if runtime.state != "accepted":
            outcome.kept.append(task_id)
            continue

        def mutate(snapshot, tid=task_id):
            rt = next(t for t in snapshot.tasks if t.task_id == tid)
            rt.state = "waiting"                    # 先离开 accepted
            rt.verification_status = "stale"        # 再把验收标过期
            rt.generation += 1                      # 改版后重新执行算新代际

        ctx.commit(reason=f"改版失效：{reason}", actor=actor, actor_type="human",
                   mutate=mutate, task_id=task_id)
        outcome.invalidated.append(task_id)

    demote_unready(ctx, plan, manifest, reason=reason, actor=actor)
    return outcome


def demote_unready(ctx: RunContext, plan: Plan, manifest, *,
                   reason: str, actor: str = "YOU") -> list[str]:
    """把"前驱不再全部验收"的 ready 任务降回 waiting。

    改版会让某个已验收任务回退，但它已经解锁过的后继还停在 ready——
    这时它们其实不该再可派。不降级的话，界面会显示"可以开工"，点下去却因为
    输入不存在而失败。
    """
    demoted: list[str] = []
    for task in plan.tasks:
        if ctx.state_of(task.task_id) != "ready":
            continue
        predecessors = manifest.predecessors.get(task.task_id, [])
        if all(ctx.state_of(p) == "accepted" for p in predecessors):
            continue
        ctx.transition(task.task_id, "waiting",
                       reason=f"前驱因改版失效：{reason}", actor=actor, actor_type="human")
        demoted.append(task.task_id)
    return demoted


def supersede_plan(old: Plan, new_content: dict, *, object_id: str | None = None) -> Plan:
    """生成下一版计划（version + 1，内容指纹由契约重新校验）。

    P3 不负责决定"改什么"——那是 Daedalus 的事。这里只提供"版本前进"这一步，
    避免调用方各自拼 content_hash。
    """
    from olympus.contracts.models import Task, fingerprint

    tasks = [Task.model_validate(item) for item in new_content["tasks"]]
    payload = {
        **old.content,
        "tasks": [
            {"task_id": t.task_id, "version": t.version, "content_hash": t.content_hash}
            for t in tasks
        ],
    }
    content = {**payload}
    return old.model_copy(update={
        "object_id": object_id or old.object_id,
        "version": old.version + 1,
        "content": content,
        "content_hash": fingerprint(content),
        "tasks": tasks,
    })

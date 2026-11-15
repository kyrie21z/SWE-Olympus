"""从持久化快照与事件重建 `RunContext`。

为什么可以只靠这两样东西重建？

因为 `Event.snapshot` 是**同一事务里**写下的完整 Run 投影，而 `Store.commit`
保证快照与事件一起提交。所以最后一条事件里的 snapshot 就是权威状态，
前面的事件构成可核对的链条。

重建前逐条核对，任何一条不满足都抛 `RecoveryGap`：

1. 事件条数 == 快照的 `event_seq`（不能少，也不能多）；
2. 事件序号从 1 开始连续递增；
3. `state_revision` 只能原地不动（签认类动作）或 +1，不能跳变；
4. 末条事件的 snapshot 必须与当前快照完全一致。

第 3 条容易被忽略：批准/驳回是"产生事件但不改内容"的动作，
它的 `state_revision` 不递增——恢复逻辑如果假设"一条事件必涨一次修订号"，
就会把正常的批准记录判成损坏。
"""

from __future__ import annotations

from olympus.contracts.models import Event, Run
from olympus.orchestrator.state_machine import RunContext
from olympus.storage.sqlite import Store

from .errors import RecoveryGap


def rebuild_context(store: Store, run_id: str) -> RunContext:
    """读取快照与全部事件，核对通过后返回可用的运行态。

    未知运行由 `Store.snapshot` 抛 `KeyError`，由调用方映射成 404。
    """
    run = store.snapshot(run_id)
    events = store.events(run_id, 0)
    verify_chain(run, events)

    ctx = RunContext(run=run)
    ctx.events.extend(events)
    # 让恢复后新产生的事件编号接着已有序列走，避免 event_id 与历史重复。
    ctx._seq = len(events)  # noqa: SLF001 - 单一入口内部状态，恢复时需要对齐
    return ctx


def verify_chain(run: Run, events: list[Event]) -> None:
    """只做核对，不修改任何对象。可单独用于启动自检。"""
    if run.event_seq != len(events):
        raise RecoveryGap(
            run.run_id,
            "快照序号与已存事件条数不一致",
            detail={"run_event_seq": run.event_seq, "stored_events": len(events)},
        )

    revisions: list[int] = []
    for index, event in enumerate(events, start=1):
        if event.run_id != run.run_id:
            raise RecoveryGap(
                run.run_id,
                "事件不属于该运行",
                detail={"position": index, "event_run_id": event.run_id},
            )
        if event.event_seq != index:
            raise RecoveryGap(
                run.run_id,
                "事件序号不连续",
                detail={"position": index, "event_seq": event.event_seq, "expected": index},
            )
        revisions.append(event.state_revision)

    if not revisions:
        if run.state_revision != 0:
            raise RecoveryGap(
                run.run_id,
                "没有任何事件，但快照修订号不为零",
                detail={"state_revision": run.state_revision},
            )
        return

    for previous, current in zip(revisions, revisions[1:]):
        if current - previous not in (0, 1):
            raise RecoveryGap(
                run.run_id,
                "事件修订号跳变",
                detail={"previous": previous, "current": current},
            )

    if revisions[-1] != run.state_revision:
        raise RecoveryGap(
            run.run_id,
            "末条事件修订号与当前快照不一致",
            detail={"last_event_revision": revisions[-1], "run_state_revision": run.state_revision},
        )

    if events[-1].snapshot != run:
        raise RecoveryGap(
            run.run_id,
            "末条事件的快照与当前快照不一致",
            detail={"last_event_seq": events[-1].event_seq},
        )

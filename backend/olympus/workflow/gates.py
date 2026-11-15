"""LangGraph 阶段控制与人工关口（issue #6）。

issue 原句：

> 实现 LangGraph 阶段控制和**准备→持久化等待→原子接纳**的人工关口，
> 绑定 REQ/DS/PLAN 及必要报告。

为什么必须拆成三个节点，而不是一个"提问并等待"：

LangGraph 的 `interrupt()` 恢复会**重执行所在节点开头**（官方文档原文）。
如果准备与等待在一个节点里，恢复时会重新生成问题、重占预算、拿到一张和上次不同的卡。
拆开之后：

| 节点 | 职责 | 恢复时会发生什么 |
| --- | --- | --- |
| `prepare` | 生成并落盘请求，绑定 run/version/state_revision | 幂等：已存在就返回同一个 request_id |
| `await_decision` | 只读请求并 `interrupt()` | 只读，不重新生成 |
| `accept` | 校验版本仍有效，委托 CommandBus 提交 | 版本变了标 `stale`，不把旧答案搬到新对象 |

图只负责"停在哪儿、接着去哪儿"；业务判据仍在 `CommandBus`（单一实现）。
"""

from __future__ import annotations

import threading
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from olympus.commands.core.bus import CommandBus
from olympus.contracts.models import Command as DomainCommand
from olympus.human.store import HumanRequestStore, RequestKind

# 关口动作与命令的对应关系
KIND_TO_ACTION: dict[str, str] = {
    "design_approval": "approve_design",
    "plan_approval": "approve_plan",
    "delivery_confirm": "confirm_delivery",
}


class GateState(TypedDict, total=False):
    """图的流转数据。业务状态在 RunContext 里，这里只放"这一关走到哪一步"。"""

    run_id: str
    kind: RequestKind
    target_id: str
    expected_version: int
    payload: dict[str, Any]
    req_ref: dict[str, Any]
    ds_ref: dict[str, Any]
    plan_ref: dict[str, Any]
    required_reports: list[str]
    available_reports: list[str]
    request_id: str
    decision: str            # approve / reject
    note: str
    receipt_status: str
    receipt_reason: str


def build_gate_graph(bus: CommandBus, store: HumanRequestStore, *,
                     checkpointer: Any | None = None):
    """构造一条"单关口"的图：prepare → await → accept。"""

    def prepare(state: GateState) -> GateState:
        request = store.prepare(
            run_id=state["run_id"],
            kind=state["kind"],
            target_id=state["target_id"],
            expected_version=state["expected_version"],
            expected_state_revision=bus.ctx.run.state_revision,
            payload=state.get("payload", {}),
            req_ref=state.get("req_ref"),
            ds_ref=state.get("ds_ref"),
            plan_ref=state.get("plan_ref"),
            required_reports=state.get("required_reports", []),
            available_reports=state.get("available_reports", []),
        )
        return {"request_id": request.request_id}

    def await_decision(state: GateState) -> GateState:
        """只读已存请求并中断。恢复时重跑本节点，但**不会重建请求**。"""
        request = store.load(state["request_id"])
        answer = interrupt({
            "request_id": request.request_id,
            "kind": request.kind,
            "expected_state_revision": request.expected_state_revision,
            "payload": request.payload,
        })
        if not isinstance(answer, dict) or answer.get("decision") not in {"approve", "reject"}:
            raise ValueError("an explicit approve/reject decision is required")
        return {
            "decision": str(answer["decision"]),
            "note": str(answer.get("note", "")),
        }

    def accept(state: GateState) -> GateState:
        request = store.load(state["request_id"])

        # 先校验：请求绑定的是不是仍然是当前内容版本。
        # 内容变了就作废这张请求，绝不把旧答案搬到新对象上。
        if not store.is_current(request.request_id,
                                state_revision=bus.ctx.run.state_revision,
                                version=state["expected_version"]):
            store.mark_stale(request.request_id)
            return {"receipt_status": "rejected", "receipt_reason": "stale"}

        # 契约的 Command.action 里没有"驳回"这一项，所以驳回**不产生命令**，
        # 只记录一条事件并让请求作废——不硬塞一个契约外的动作。
        if state.get("decision") == "reject":
            note = state.get("note") or "未附理由"
            bus.ctx.commit(reason=f"驳回：{note}"[:160], actor="YOU", actor_type="human",
                           mutate=lambda snap: None)
            store.accept(request.request_id)
            return {"receipt_status": "rejected", "receipt_reason": "rejected_by_human"}

        action = KIND_TO_ACTION[state["kind"]]
        parameters: dict[str, Any] = {}
        if state.get("note"):
            parameters["reason"] = state["note"]

        receipt = bus.dispatch(DomainCommand(
            operation_id=f"{request.request_id}-{action}",
            run_id=state["run_id"],
            actor="YOU",
            action=action,                          # type: ignore[arg-type]
            target_id=state["target_id"],
            expected_version=state["expected_version"],
            expected_state_revision=bus.ctx.run.state_revision,
            parameters=parameters,
        ))
        store.accept(request.request_id)
        return {"receipt_status": receipt.status, "receipt_reason": receipt.reason_code}

    graph = StateGraph(GateState)
    graph.add_node("prepare", prepare)
    graph.add_node("await_decision", await_decision)
    graph.add_node("accept", accept)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "await_decision")
    graph.add_edge("await_decision", "accept")
    graph.add_edge("accept", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


class Gate:
    """一个关口的生命周期。**必须复用同一个实例**，否则 checkpointer 丢了恢复不了。"""

    def __init__(self, bus: CommandBus, store: HumanRequestStore, *,
                 checkpointer: Any | None = None, thread_id: str = "gate-1") -> None:
        self.bus = bus
        self.store = store
        self.thread_id = thread_id
        self._graph = build_gate_graph(bus, store, checkpointer=checkpointer)
        # 同一 thread 的恢复必须串行：并发 resume 会争同一份 checkpoint
        self._lock = threading.RLock()

    @property
    def config(self) -> dict:
        return {"configurable": {"thread_id": self.thread_id}}

    def start(self, *, kind: RequestKind, target_id: str, expected_version: int,
              payload: dict[str, Any] | None = None,
              req_ref: dict[str, Any] | None = None,
              ds_ref: dict[str, Any] | None = None,
              plan_ref: dict[str, Any] | None = None,
              required_reports: list[str] | None = None,
              available_reports: list[str] | None = None) -> dict[str, Any]:
        """第一次调用：跑到中断点，返回挂起信息。

        `req_ref/ds_ref/plan_ref` 与 `required_reports` 一起构成这一关的输入依据；
        报告不齐会在 `prepare` 处直接抛错，不会走到中断点再让人发现。
        """
        state: GateState = {
            "run_id": self.bus.ctx.run.run_id,
            "kind": kind,
            "target_id": target_id,
            "expected_version": expected_version,
            "payload": payload or {},
            "req_ref": req_ref,
            "ds_ref": ds_ref,
            "plan_ref": plan_ref,
            "required_reports": required_reports or [],
            "available_reports": available_reports or [],
        }
        with self._lock:
            return _unwrap(self._graph.invoke(state, config=self.config))

    def resume(self, *, answer: dict[str, Any]) -> dict[str, Any]:
        """同一个 thread_id 恢复，提交人的回答。"""
        with self._lock:
            return _unwrap(self._graph.invoke(Command(resume=answer), config=self.config))


def _unwrap(result: Any) -> dict[str, Any]:
    if isinstance(result, dict) and "__interrupt__" in result:
        interrupts = result["__interrupt__"]
        first = interrupts[0] if interrupts else None
        return {"status": "interrupted", "request": getattr(first, "value", first)}
    if isinstance(result, dict):
        return {"status": "completed", **result}
    return {"status": "unknown", "raw": str(result)}

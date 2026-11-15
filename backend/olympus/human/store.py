"""人工请求的持久化存储（issue #6：`prepare → 持久化等待 → 原子接纳`）。

为什么请求必须先落盘再等待：

LangGraph 的 `interrupt()` 恢复时会**重执行所在节点开头**。如果"生成问题"和"等待"
在同一个节点里，恢复就会重新生成一张问题卡——编号变了、预算又扣一次、
人答的是上一版。所以准备与等待必须分开，而分开的前提是**请求本身是持久化对象**：

```
prepare   → 生成并落盘（绑定 run/version/state_revision）
await     → 只读已存请求，interrupt
accept    → 校验版本仍然有效，原子接纳
```

存储介质：默认 JSON 文件（进程重启可读）；传 `path=None` 时退化为内存（测试用）。
写入用"临时文件 + 原子替换"，避免半个文件被读到。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

RequestKind = Literal["design_approval", "plan_approval", "delivery_confirm"]
RequestStatus = Literal["pending", "answered", "accepted", "stale"]


@dataclass(frozen=True)
class HumanRequest:
    request_id: str
    run_id: str
    kind: RequestKind
    target_id: str
    expected_version: int
    expected_state_revision: int
    payload: dict[str, Any] = field(default_factory=dict)
    # issue #6："绑定 REQ/DS/PLAN 及必要报告"——关口的输入不是"当前状态"，
    # 而是"哪一版需求、哪一版设计、哪一版计划、哪些报告支撑"。缺一项就不能开。
    req_ref: dict[str, Any] | None = None
    ds_ref: dict[str, Any] | None = None
    plan_ref: dict[str, Any] | None = None
    required_reports: list[str] = field(default_factory=list)
    status: RequestStatus = "pending"
    answer: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class HumanRequestStore:
    """请求的唯一存储点。`prepare` 幂等：同一 (run, kind, target) 只建一次。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._items: dict[str, HumanRequest] = {}
        if self._path and self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._items = {k: HumanRequest(**v) for k, v in raw.items()}

    # ---------------------------------------------------------------- 写
    def prepare(self, *, run_id: str, kind: RequestKind, target_id: str,
                expected_version: int, expected_state_revision: int,
                payload: dict[str, Any] | None = None,
                req_ref: dict[str, Any] | None = None,
                ds_ref: dict[str, Any] | None = None,
                plan_ref: dict[str, Any] | None = None,
                required_reports: list[str] | None = None,
                available_reports: list[str] | None = None) -> HumanRequest:
        """开一个关口。必要报告不齐就直接拒绝——不能让人在没有依据的情况下做决定。"""
        missing = [r for r in (required_reports or [])
                   if r not in (available_reports or [])]
        if missing:
            raise ValueError(f"必要报告缺失，不能开启关口：{missing}")

        existing = self.find(run_id=run_id, kind=kind, target_id=target_id, status="pending")
        if existing:
            if (existing.expected_version == expected_version and
                    existing.expected_state_revision == expected_state_revision and
                    existing.req_ref == req_ref and existing.ds_ref == ds_ref and
                    existing.plan_ref == plan_ref and
                    existing.required_reports == list(required_reports or [])):
                return existing
            # Same target ID is not the same approval package after a revision.
            self.mark_stale(existing.request_id)
        request = HumanRequest(
            request_id=f"HR-{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            kind=kind,
            target_id=target_id,
            expected_version=expected_version,
            expected_state_revision=expected_state_revision,
            payload=dict(payload or {}),
            req_ref=req_ref,
            ds_ref=ds_ref,
            plan_ref=plan_ref,
            required_reports=list(required_reports or []),
        )
        self._items[request.request_id] = request
        self._flush()
        return request

    def answer(self, request_id: str, answer: dict[str, Any]) -> HumanRequest:
        request = self.load(request_id)
        if request.status not in ("pending",):
            return request                        # 幂等：已答过就原样返回
        updated = replace(request, status="answered", answer=dict(answer))
        self._items[request_id] = updated
        self._flush()
        return updated

    def accept(self, request_id: str) -> HumanRequest:
        """标记已接纳。版本校验由调用方在做决定之前完成（见 `is_current`）。"""
        request = self.load(request_id)
        if request.status == "accepted":
            return request                         # 幂等
        updated = replace(request, status="accepted")
        self._items[request_id] = updated
        self._flush()
        return updated

    def mark_stale(self, request_id: str) -> HumanRequest:
        """内容已变，旧请求作废——但不删除，历史可查。"""
        updated = replace(self.load(request_id), status="stale")
        self._items[request_id] = updated
        self._flush()
        return updated

    def is_current(self, request_id: str, *, state_revision: int, version: int) -> bool:
        """请求绑定的内容版本是否仍是当前版本。"""
        request = self.load(request_id)
        return (request.expected_state_revision == state_revision
                and request.expected_version == version)

    # ---------------------------------------------------------------- 读
    def load(self, request_id: str) -> HumanRequest:
        if request_id not in self._items:
            raise KeyError(f"unknown request {request_id}")
        return self._items[request_id]

    def find(self, *, run_id: str, kind: str, target_id: str,
             status: str | None = None) -> HumanRequest | None:
        for request in self._items.values():
            if (request.run_id, request.kind, request.target_id) != (run_id, kind, target_id):
                continue
            if status is None or request.status == status:
                return request
        return None

    def pending(self, run_id: str) -> list[HumanRequest]:
        return [r for r in self._items.values()
                if r.run_id == run_id and r.status == "pending"]

    def all(self) -> list[HumanRequest]:
        return list(self._items.values())

    # ---------------------------------------------------------------- 落盘
    def _flush(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({k: v.to_dict() for k, v in self._items.items()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)               # 原子替换：不会读到半个文件

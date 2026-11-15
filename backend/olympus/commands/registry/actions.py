"""命令注册表：动作 → 允许角色。

issue #6 要求"处理器注册"与"核心命令核"分开：
- `commands/core/` 负责鉴权、版本、幂等、CAS 与前置状态（机制）
- `commands/registry/` 负责"有哪些动作、谁能发、什么时候能发"（策略）

这样 P5 加运行期处理器时，只改注册表 + 注册一个 handler，不动核心核。

`NOT_READY` 的含义：动作已登记但本段尚未实现处理器。按 issue #6 的要求，
提示重试 / 改派 / 风险接受 / 变更修复这四类**明确移交 P5（#9）**，
本段只保留登记并返回 `NOT_READY`，不假装实现。
"""

from __future__ import annotations

from typing import Literal

Action = Literal[
    "approve_design",
    "approve_plan",
    "start",
    "pause",
    "resume",
    "retry",
    "reassign",
    "accept_risk",
    "request_change",
    "confirm_delivery",
]

# P3 本段实现的动作
CORE_ACTIONS: tuple[str, ...] = (
    "approve_design",
    "approve_plan",
    "start",
    "pause",
    "resume",
    "confirm_delivery",
)

# 明确移交 P5（#9）的运行期动作：登记但未注册处理器
DEFERRED_ACTIONS: tuple[str, ...] = (
    "retry",
    "reassign",
    "accept_risk",
    "request_change",
)

# 每个动作允许的角色。YOU 永远是最终决定者；Zeus 只允许程序侧动作。
ACTION_ACTORS: dict[str, frozenset[str]] = {
    "approve_design": frozenset({"YOU"}),
    "approve_plan": frozenset({"YOU"}),
    "start": frozenset({"YOU"}),
    "pause": frozenset({"YOU", "Zeus"}),
    "resume": frozenset({"YOU"}),
    "confirm_delivery": frozenset({"YOU"}),
    "retry": frozenset({"YOU"}),
    "reassign": frozenset({"YOU", "Daedalus"}),
    "accept_risk": frozenset({"YOU"}),
    "request_change": frozenset({"YOU"}),
}

# 动作的前置 run 状态（用于 allowed_actions 与拒绝原因）
ACTION_PRECONDITION: dict[str, frozenset[str]] = {
    "approve_design": frozenset({"draft"}),
    "approve_plan": frozenset({"draft"}),
    "start": frozenset({"draft"}),
    "pause": frozenset({"active"}),
    "resume": frozenset({"paused"}),
    "confirm_delivery": frozenset({"active", "awaiting_confirmation"}),
    "request_change": frozenset({"active", "paused"}),
    "retry": frozenset({"active", "paused"}),
    "reassign": frozenset({"draft", "active", "paused"}),
    "accept_risk": frozenset({"active", "paused"}),
}


def actors_for(action: str) -> frozenset[str]:
    return ACTION_ACTORS.get(action, frozenset())


def is_deferred(action: str) -> bool:
    return action in DEFERRED_ACTIONS

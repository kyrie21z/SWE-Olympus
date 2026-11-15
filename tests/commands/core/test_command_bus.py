"""命令核的单测（issue #6 的 `tests/commands/core/`）。

只测"命令核应该保证什么"，不重复场景测试里的时间线：

1. 六道闸门各自能拒；
2. `allowed_actions` 与实际状态一致（禁用必须带原因）；
3. 未注册动作返回 `NOT_READY`，P5 注册后立即可用。
"""

from __future__ import annotations

import pytest
from olympus.commands.core.bus import CommandBus
from olympus.contracts.models import Command, ContractBundle
from olympus.orchestrator.scheduler import Scheduler
from olympus.orchestrator.state_machine import RunContext
from olympus.planning.service import PlanService


@pytest.fixture
def world(bundle: ContractBundle):
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    ctx = RunContext(run=bundle.run)
    sched = Scheduler(plan=bundle.plan, manifest=package.manifest, ctx=ctx)
    return ctx, sched, CommandBus(ctx=ctx, scheduler=sched), bundle.plan


def make(ctx, action, *, actor="YOU", op="op-1", target="RUN-campus", version=1,
         revision=None, parameters=None):
    return Command(
        operation_id=op, run_id=ctx.run.run_id, actor=actor, action=action,
        target_id=target, expected_version=version,
        expected_state_revision=revision if revision is not None else ctx.run.state_revision,
        parameters=parameters or {},
    )


def test_catalog_lists_all_actions_with_implemented_flag(world):
    _ctx, _sched, bus, _plan = world
    rows = {r["action"]: r for r in bus.catalog()}
    assert rows["approve_plan"]["implemented"] is True
    assert rows["retry"]["implemented"] is False
    assert "P5" in rows["retry"]["effect"]


def test_allowed_actions_explains_every_denial(world):
    """禁用必须带原因——界面要能显示"为什么点不了"。"""
    ctx, sched, bus, _plan = world
    for row in bus.allowed_actions("YOU"):
        if not row["enabled"]:
            assert row["reasons"], f"{row['action']} 被禁用却没给原因"

    enabled = {r["action"] for r in bus.allowed_actions("YOU") if r["enabled"]}
    assert enabled == {"approve_design", "approve_plan", "start"}, "draft 时只有这三件事能做"

    bus.dispatch(make(ctx, "start"))
    sched.reconcile()
    enabled = {r["action"] for r in bus.allowed_actions("YOU") if r["enabled"]}
    assert "pause" in enabled and "start" not in enabled


def test_forbidden_actor_lists_allowed_ones(world):
    ctx, _sched, bus, _plan = world
    receipt = bus.dispatch(make(ctx, "start", actor="Hephaestus"))
    assert receipt.reason_code == "forbidden"
    assert "YOU" in bus.notes["op-1"]


def test_contract_rejects_unknown_action_before_bus(world):
    """契约的 Literal 先把关：未知动作根本构造不出来，命令核的 bad_args 只是兜底。"""
    from pydantic import ValidationError

    ctx, _sched, _bus, _plan = world
    with pytest.raises(ValidationError):
        Command(operation_id="op-x", run_id=ctx.run.run_id, actor="YOU",
                action="fly_to_moon", target_id="RUN-campus", expected_version=1,
                expected_state_revision=ctx.run.state_revision, parameters={})


def test_deferred_action_is_not_ready(world):
    ctx, sched, bus, _plan = world
    bus.dispatch(make(ctx, "start", op="op-start"))
    sched.reconcile()
    sched.acquire("T01")
    ctx.transition("T01", "failed", reason="检查不通过", actor="Zeus")

    receipt = bus.dispatch(make(ctx, "retry", op="op-retry", target="T01"))
    assert receipt.status == "rejected"
    assert receipt.reason_code == "not_ready"


def test_registered_handler_overrides_not_ready(world):
    """P5 注册后立即可用，本文件一行不改。"""
    ctx, sched, bus, _plan = world
    bus.dispatch(make(ctx, "start", op="op-start"))
    sched.reconcile()

    def handler(command, context, scheduler):
        return "accepted", "ok", f"模拟 P5 处理 {command.action}"

    bus.register("retry", handler, {"YOU"}, "任务 failed/blocked", "回到 ready")
    receipt = bus.dispatch(make(ctx, "retry", op="op-retry", target="T01"))
    assert receipt.status == "accepted"
    assert "模拟 P5" in bus.notes["op-retry"]

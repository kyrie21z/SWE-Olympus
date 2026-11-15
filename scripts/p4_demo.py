"""P4 无界面演示：使用构造模型响应和真实本地 Python 检查，不写入远端。"""

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from olympus.agents.argus import ArgusReviewer  # noqa: E402
from olympus.agents.athena import AthenaDesigner, DesignWorkflow  # noqa: E402
from olympus.agents.daedalus import DaedalusPlanner  # noqa: E402
from olympus.agents.hephaestus import HephaestusImplementer  # noqa: E402
from olympus.agents.momus.acceptance import FrozenAcceptance  # noqa: E402
from olympus.agents.momus.design import DesignReviewer  # noqa: E402
from olympus.agents.momus.planning import MomusPlanReviewer, PlanningWorkflow  # noqa: E402
from olympus.agents.runtime import AgentRunner, MockProvider  # noqa: E402
from olympus.commands.core.bus import CommandBus  # noqa: E402
from olympus.contracts.models import Command, ContractBundle, Run, TaskRuntime  # noqa: E402
from olympus.execution import ArtifactAcceptanceService, LocalPythonExecutor  # noqa: E402
from olympus.orchestrator.scheduler import Scheduler  # noqa: E402
from olympus.orchestrator.state_machine import RunContext  # noqa: E402
from olympus.planning.service import PlanService  # noqa: E402
from olympus.requirements_bridge import RequirementBridge  # noqa: E402
from olympus.traceability.planning import build_planning_traceability  # noqa: E402


def demonstrate(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    baseline = ContractBundle.model_validate_json((ROOT / "fixtures/contracts/valid.json").read_text("utf-8"))
    req = baseline.requirement
    simple = 'def states():\n    return ("CONFIRMED", "WAITLISTED", "CANCELLED")\n'
    simple_check = ('from packages.t01.contract import states\n'
                    'assert states() == ("CONFIRMED", "WAITLISTED", "CANCELLED")\n')
    responses = {
        "Athena:design": {"output": {"design": baseline.design.content}},
        "Momus:design_review": {"output": {"verdict": "passed", "findings": []}},
        "Daedalus:plan": {"output": {"tasks": [t.semantic_content() for t in baseline.plan.tasks]}},
        "Momus:plan_review": {"output": {"verdict": "passed", "findings": []}},
        "Hephaestus:T01": {"output": {"changes": [{"path": "packages/t01/contract.py", "content": simple}]}},
        "Hephaestus:T03": {"output": {"changes": [{"path": "packages/t03/campus.py", "content":
            (ROOT / "fixtures/execution/campus_service.py").read_text("utf-8")}]}},
        "Argus:artifact_review": {"output": {"verdict": "passed", "findings": []}},
    }
    runner = AgentRunner(MockProvider(fixtures=responses))
    bridge = RequirementBridge(allow_synthetic=True)
    bridge.admit(req)
    flow = DesignWorkflow(bridge, AthenaDesigner(runner), DesignReviewer(runner, profile="campus-v1.3"))
    run_id = "RUN-P4-" + uuid4().hex[:12]
    designed = flow.generate(req, run_id=run_id)
    if designed.status != "awaiting_approval":
        raise RuntimeError("design did not pass independent checks")
    ds = designed.design

    # A design-stage run has no PLAN yet. P3's command registry remains the authority.
    ctx = RunContext(Run(run_id=run_id, mode="mock", approved_package=[req.ref(), ds.ref()], tasks=[]))
    bus = CommandBus(ctx)
    flow.bind_approval(bus, req, ds)
    def command(action, target_id, version):
        receipt = bus.dispatch(Command(operation_id=f"demo-{action}", run_id=run_id, actor="YOU",
            action=action, target_id=target_id, expected_version=version,
            expected_state_revision=ctx.run.state_revision, parameters={}))
        if receipt.status != "accepted":
            raise RuntimeError(f"{action} rejected: {receipt.reason_code}")
    command("approve_design", ds.object_id, ds.version)
    planned = flow.plan(req, ds, PlanningWorkflow(DaedalusPlanner(runner), MomusPlanReviewer(runner=runner)))
    if planned.status != "passed":
        raise RuntimeError("plan did not pass independent checks")
    package = PlanService().prepare(req, ds, planned.plan)
    def attach_plan(snapshot):
        snapshot.approved_package = [req.ref(), ds.ref(), planned.plan.ref()]
        snapshot.tasks = [TaskRuntime(task_id=t.task_id) for t in planned.plan.tasks]
        snapshot.mode = "real_tool"
        snapshot.calls_used = runner.ledger.total_calls_accounted()
    ctx.commit(reason="synthetic demo: attach reviewed plan and authorize bounded local tools",
               actor="YOU", actor_type="human", mutate=attach_plan)
    scheduler = Scheduler(planned.plan, package.manifest, ctx)
    bus.scheduler = scheduler
    bus.bind_target(planned.plan.ref())
    command("approve_plan", planned.plan.object_id, planned.plan.version)
    command("start", run_id, planned.plan.version)

    executor = LocalPythonExecutor(scheduler, output / "executor", HephaestusImplementer(runner))
    service = ArtifactAcceptanceService(executor, ArgusReviewer(runner))
    executions = []
    for task_id, script in [("T01", simple_check), ("T03",
                          (ROOT / "fixtures/execution/check_campus.py").read_text("utf-8"))]:
        task = scheduler.tasks[task_id]
        executor.register_task(task_id, FrozenAcceptance.freeze(task, {c: script for c in task.checks}),
                               context={req.object_id: req.content, ds.object_id: ds.content})
        if scheduler.dispatch(task_id, executor) is None:
            raise RuntimeError(f"{task_id} was not dispatched")
        receipt = executor.wait(scheduler.attempt_for(task_id), timeout=30)
        status, reason = service.collect(task_id, receipt.attempt_id)
        if status != "accepted":
            raise RuntimeError(f"{task_id} not accepted: {reason}")
        executions.append({"receipt": executor.project(receipt.attempt_id), "acceptance_review": reason})

    result = {
        "source": "synthetic_fixture", "model_mode": "mock", "tool_mode": "real_tool",
        "approval": "scripted YOU fixture, not real human sign-off",
        "created_at": datetime.now(UTC).isoformat(), "design": flow.project(req, ds),
        "plan": package.model_dump(mode="json"),
        "traceability": build_planning_traceability(req, ds, planned.plan).model_dump(mode="json"),
        "executions": executions, "runtime": scheduler.projection(),
        "run_completed": False, "note": "P4 demonstration accepts T01 and T03 only; remaining tasks are not complete.",
        "events": [e.model_dump(mode="json") for e in ctx.events],
    }
    (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    destination = args.output or ROOT / ".olympus" / ("p4-demo-" + uuid4().hex[:8])
    report = demonstrate(destination)
    print(json.dumps({"report": str(destination / "report.json"), "model": report["model_mode"],
                      "tools": report["tool_mode"], "progress": report["runtime"]["progress"]},
                     ensure_ascii=True))

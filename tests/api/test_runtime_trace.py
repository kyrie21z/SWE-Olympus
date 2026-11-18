"""HTTP trace verification follows accepted runtime evidence, preserving P2's index."""

from olympus.orchestrator.recovery import RuntimeRegistry
from olympus.planning.service import PlanService
from olympus.projections.http import build_run_view, build_trace_view
from olympus.storage.sqlite import Store


def test_completed_trace_reflects_actual_task_verification_without_mutating_planning_index(tmp_path, bundle):
    store = Store(tmp_path / "state.sqlite3")
    store.create(bundle.run)
    runtime = RuntimeRegistry(store).get(bundle.run.run_id)
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    runtime.attach_plan(bundle.plan, package.manifest, requirement=bundle.requirement, design=bundle.design)
    baseline = runtime.trace.model_dump(mode="json")

    def accept(run):
        for task in run.tasks:
            task.verification_status = "passed"
            task.state = "accepted"

    runtime.ctx.commit(reason="test authoritative accepted projection", actor="Zeus", actor_type="program",
                       mutate=accept)
    view = build_run_view(runtime)
    assert all(row["accepted"] and row["verification_status"] == "passed" for row in view["trace"]["rows"])
    assert runtime.trace.model_dump(mode="json") == baseline
    assert view["trace"]["planning_index_hash"] == baseline["index_hash"]

    def accept_risk(run):
        task = run.tasks[0]
        task.state = "cancelled"
        task.verification_status = "unverified"
        task.disposition_reason = "test manual risk acceptance"
        task.disposition = "risk_accepted"

    runtime.ctx.commit(reason="test risk does not verify coverage", actor="YOU", actor_type="human",
                       mutate=accept_risk)
    trace = build_trace_view(runtime)
    affected = [row for row in trace["rows"] if bundle.run.tasks[0].task_id in row["all_tasks"]]
    assert affected and all(not row["accepted"] and row["verification_status"] != "passed" for row in affected)

"""Real SQLite restart, transaction rollback and runtime-binding regressions."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from olympus.commands.runtime import CommandService, StoreJournal
from olympus.commands.runtime.errors import CommandPersistenceUnavailable, CommandStateConflict
from olympus.contracts.models import Command
from olympus.orchestrator.recovery import RecoveryGap, RuntimeRegistry, plan_repair, verify_chain
from olympus.planning.service import PlanService
from olympus.storage.sqlite import ConcurrentCommit, Store


def seed(tmp_path, bundle):
    store = Store(tmp_path / "durable.sqlite3")
    store.create(bundle.run)
    registry = RuntimeRegistry(store)
    runtime = registry.get(bundle.run.run_id)
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    runtime.attach_plan(
        bundle.plan, package.manifest, requirement=bundle.requirement, design=bundle.design,
    )
    return store, registry, runtime


def command(runtime, action="start", operation_id="OP-durable", **changes):
    values = {
        "operation_id": operation_id, "run_id": runtime.run_id, "actor": "YOU",
        "action": action, "target_id": runtime.run_id, "expected_version": runtime.plan.version,
        "expected_state_revision": runtime.ctx.run.state_revision, "parameters": {},
    }
    return Command(**(values | changes))


def service(store, registry):
    return CommandService(registry, journal=StoreJournal(store))


def test_start_batch_and_plan_trace_survive_fresh_registry(tmp_path, bundle):
    store, registry, runtime = seed(tmp_path, bundle)
    start = command(runtime)
    first = service(store, registry).submit(start)
    assert first.receipt.status == "accepted"
    assert first.durability == "sqlite"
    assert first.recorded_events > 1  # active plus all ready promotions, not just the last event
    verify_chain(store.snapshot(runtime.run_id), store.events(runtime.run_id))

    reopened = Store(store.path)
    restarted = RuntimeRegistry(reopened)
    recovered = restarted.get(runtime.run_id)
    assert recovered.has_plan and recovered.bus is not None
    assert recovered.ctx.run == runtime.ctx.run
    assert recovered.trace == runtime.trace
    assert recovered.plan == runtime.plan
    replay = service(reopened, restarted).submit(start)
    assert replay.receipt == first.receipt
    assert replay.recorded_events == 0
    assert len(reopened.events(runtime.run_id)) == first.recorded_events


def test_rejected_receipt_stays_idempotent_after_other_commands_and_restart(tmp_path, bundle):
    store, registry, runtime = seed(tmp_path, bundle)
    pause = command(runtime, "pause", "OP-rejected")
    first = service(store, registry).submit(pause)
    assert first.receipt.status == "rejected"
    assert store.events(runtime.run_id) == []
    service(store, registry).submit(command(runtime))
    restarted = RuntimeRegistry(Store(store.path))
    again = service(restarted.store, restarted).submit(pause)
    assert again.receipt == first.receipt
    assert restarted.get(runtime.run_id).ctx.run.status == "active"


def test_changed_payload_cannot_replace_original_operation_receipt(tmp_path, bundle):
    store, registry, runtime = seed(tmp_path, bundle)
    original = command(runtime)
    first = service(store, registry).submit(original)
    restarted = RuntimeRegistry(Store(store.path))
    changed = original.model_copy(update={"parameters": {"prompt": "different"}})
    refused = service(restarted.store, restarted).submit(changed)
    assert refused.receipt.reason_code == "conflict"
    assert refused.receipt.status == "rejected"
    assert service(restarted.store, restarted).submit(original).receipt == first.receipt
    assert len(store.events(runtime.run_id)) == first.recorded_events


def test_receipt_failure_rolls_back_events_metadata_and_live_runtime(tmp_path, bundle):
    store, registry, runtime = seed(tmp_path, bundle)
    original = runtime.checkpoint()
    with store.connection() as db:
        db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON operations "
                   "BEGIN SELECT RAISE(ABORT,'injected receipt failure'); END")
    cmd = command(runtime)
    with pytest.raises(CommandPersistenceUnavailable):
        service(store, registry).submit(cmd)
    assert runtime.ctx.run == original["run"] == store.snapshot(runtime.run_id)
    assert runtime.ctx.events == [] == store.events(runtime.run_id)
    assert runtime.export_state() == original["state"] == store.load_runtime(runtime.run_id)
    assert runtime.bus._receipts == original["receipts"]
    with store.connection() as db:
        db.execute("DROP TRIGGER fail_receipt")
    assert service(store, registry).submit(cmd).receipt.status == "accepted"


def test_runtime_metadata_failure_is_atomic_with_receipt_and_batch(tmp_path, bundle):
    store, registry, runtime = seed(tmp_path, bundle)
    with store.connection() as db:
        db.execute("CREATE TRIGGER fail_metadata BEFORE UPDATE ON runtime_bindings "
                   "BEGIN SELECT RAISE(ABORT,'injected metadata failure'); END")
    cmd = command(runtime)
    with pytest.raises(CommandPersistenceUnavailable):
        service(store, registry).submit(cmd)
    assert store.command_receipt(cmd) is None
    assert runtime.ctx.run.status == store.snapshot(runtime.run_id).status == "draft"
    assert store.events(runtime.run_id) == []


def test_assignments_and_trail_are_restored(tmp_path, bundle):
    store, registry, runtime = seed(tmp_path, bundle)
    service(store, registry).submit(command(runtime))
    reassign = command(
        runtime, "reassign", "OP-reassign", target_id="T01", parameters={"assignee": "Hermes"},
    )
    first = service(store, registry).submit(reassign)
    assert first.receipt.status == "accepted"
    restarted = RuntimeRegistry(Store(store.path)).get(runtime.run_id)
    assert restarted.handlers.assignments(runtime.run_id) == {"T01": "Hermes"}
    assert restarted.handlers.trail(runtime.run_id) == runtime.handlers.trail(runtime.run_id)


def test_inflight_reservations_are_retained_without_redispatch(tmp_path, bundle):
    store, registry, runtime = seed(tmp_path, bundle)
    service(store, registry).submit(command(runtime))
    assert runtime.scheduler.acquire("T01") is not None
    runtime.scheduler._attempts["T01"] = "ATT-still-unknown"
    runtime.scheduler.register_incident("T01", "outcome_unknown", "process interrupted")
    runtime.persist()
    events = len(store.events(runtime.run_id))
    restarted = RuntimeRegistry(Store(store.path)).get(runtime.run_id)
    assert restarted.ctx.state_of("T01") == "running"
    assert restarted.scheduler.permit_for("T01") == runtime.scheduler.permit_for("T01")
    assert restarted.scheduler.attempt_for("T01") == "ATT-still-unknown"
    assert restarted.scheduler._executor_load == runtime.scheduler._executor_load
    assert restarted.scheduler.acquire("T01") is None
    assert len(store.events(runtime.run_id)) == events


def test_repair_plan_and_rewired_dependencies_survive_restart(tmp_path, bundle):
    store, registry, runtime = seed(tmp_path, bundle)
    service(store, registry).submit(command(runtime))
    outcome = plan_repair(runtime, "T03", failure_ref="FAIL-1", reason="repair upstream")
    runtime.persist()
    restarted = RuntimeRegistry(Store(store.path)).get(runtime.run_id)
    assert restarted.plan == runtime.plan
    assert restarted.plan.version == outcome.plan_version
    assert restarted.repairs == runtime.repairs
    assert restarted.trace == runtime.trace
    assert restarted.scheduler.manifest == runtime.scheduler.manifest
    assert any(task.task_id == outcome.repair_task_id for task in restarted.plan.tasks)
    verify_chain(store.snapshot(runtime.run_id), store.events(runtime.run_id))


def test_program_batches_cannot_overwrite_another_committed_baseline(tmp_path, bundle):
    store, registry, runtime = seed(tmp_path, bundle)
    baseline = runtime.ctx.run.model_copy(deep=True)
    service(store, registry).submit(command(runtime))
    with pytest.raises(ConcurrentCommit):
        store.append_events(runtime.run_id, [], expected=baseline, runtime_state={"wrong": True})
    assert store.load_runtime(runtime.run_id)["plan"] == runtime.plan.model_dump(mode="json")


@pytest.mark.parametrize("same_operation", [False, True])
def test_two_process_contexts_share_a_single_atomic_command_result(tmp_path, bundle, same_operation):
    store, _, runtime = seed(tmp_path, bundle)
    registries = [RuntimeRegistry(Store(store.path)) for _ in range(2)]
    services = [service(item.store, item) for item in registries]
    gate = threading.Barrier(2, timeout=10)
    for item in services:
        lookup = item._journal.lookup

        def synchronized_lookup(command, original=lookup):
            previous = original(command)
            gate.wait()
            return previous

        item._journal.lookup = synchronized_lookup
    commands = [command(runtime, operation_id="OP-race" if same_operation else f"OP-race-{i}")
                for i in range(2)]

    def submit(index):
        try:
            return services[index].submit(commands[index])
        except CommandStateConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert sum(item is not None for item in results) == (2 if same_operation else 1)
    if same_operation:
        assert results[0].receipt == results[1].receipt
        assert sorted(item.recorded_events for item in results)[0] == 0
    for registry in registries:
        recovered = registry.get(runtime.run_id)
        assert recovered.ctx.events == store.events(runtime.run_id)
        assert recovered.ctx.run == store.snapshot(runtime.run_id)


def test_rejected_handler_does_not_leave_partial_effects(tmp_path, bundle):
    store, registry, runtime = seed(tmp_path, bundle)

    def broken_handler(cmd, ctx, scheduler):
        ctx.set_run_status("active", reason="partial transition", actor="YOU")
        raise ValueError("reject after partial transition")

    runtime.bus.register("start", broken_handler)
    result = service(store, registry).submit(command(runtime))
    assert result.receipt.status == "rejected"
    assert result.receipt.state_revision == 0
    assert runtime.ctx.run == store.snapshot(runtime.run_id)
    assert runtime.ctx.run.status == "draft"
    assert store.events(runtime.run_id) == []


def test_failed_plan_binding_rolls_back_the_live_binding(tmp_path, bundle):
    store = Store(tmp_path / "failed-binding.sqlite3")
    store.create(bundle.run)
    runtime = RuntimeRegistry(store).get(bundle.run.run_id)
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    with store.connection() as db:
        db.execute("CREATE TRIGGER fail_binding BEFORE INSERT ON runtime_bindings "
                   "BEGIN SELECT RAISE(ABORT,'injected binding failure'); END")
    with pytest.raises(Exception, match="injected"):
        runtime.attach_plan(bundle.plan, package.manifest)
    assert runtime.plan is None and runtime.scheduler is None and runtime.bus is None
    assert store.load_runtime(runtime.run_id) is None


def test_corrupted_saved_plan_is_not_cached_as_a_usable_runtime(tmp_path, bundle):
    store, _, runtime = seed(tmp_path, bundle)
    saved = store.load_runtime(runtime.run_id)
    saved["plan"]["content_hash"] = "0" * 64
    store.save_runtime(runtime.run_id, saved)
    restarted = RuntimeRegistry(Store(store.path))
    with pytest.raises(RecoveryGap):
        restarted.get(runtime.run_id)
    assert restarted.peek(runtime.run_id) is None


def test_command_flushes_pending_executor_progress_without_splitting_runtime(tmp_path, bundle):
    store, registry, runtime = seed(tmp_path, bundle)
    service(store, registry).submit(command(runtime))
    # P4 can account a model response before the host next persists the batch.
    runtime.ctx.commit(reason="background model accounted", actor="Zeus", actor_type="program",
                       mutate=lambda run: setattr(run, "calls_used", 1))
    pause = command(runtime, "pause", "OP-pause-during-review")
    result = service(store, registry).submit(pause)
    assert result.receipt.status == "accepted"
    assert registry.get(runtime.run_id) is runtime
    assert runtime.ctx.run.calls_used == 1
    assert runtime.ctx.run.status == "paused"
    assert runtime.ctx.run == store.snapshot(runtime.run_id)
    verify_chain(store.snapshot(runtime.run_id), store.events(runtime.run_id))

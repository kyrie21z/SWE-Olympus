"""Keep exact unchanged frozen contracts while invalidating changes and descendants."""

from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.agents.runtime import AgentRunner, OutputSchemaError
from olympus.app import create_app
from olympus.contracts.models import Ref
from olympus.storage.sqlite import Store
from olympus.workflows.acceptance import _task_manifest, migrate_acceptance_checkpoint, validate_check

from test_acceptance_corrections import plan_with, task_with
from test_browser_host import wait_for_stage
from test_direct_plan_revision import DirectRevisionProvider, prepared


@pytest.fixture
def forked_plan(bundle):
    specs = [("A", []), ("B", ["A"]), ("C", ["B"]), ("D", []), ("E", ["C"]), ("F", ["D"])]
    tasks = [task_with(bundle.plan.tasks[0], task_id=task_id, depends_on=dependencies,
                       inputs=[f"artifact:{dependency}" for dependency in dependencies],
                       outputs=[f"artifact:{task_id}"], checks=[f"check:{task_id}"])
             for task_id, dependencies in specs]
    plan = plan_with(bundle.plan, tasks)
    frozen = {task.task_id: FrozenAcceptance.freeze(task, {task.checks[0]: "assert 1 + 1 == 2\n"}).model_dump(mode="json")
              for task in tasks}
    return plan, frozen


def test_change_invalidates_task_and_every_descendant_but_keeps_independent_exact_contracts(forked_plan):
    baseline, frozen = forked_plan
    tasks = [task_with(task, goal="Changed implementation") if task.task_id == "B" else task for task in baseline.tasks]
    changed = plan_with(baseline, tasks)
    snapshot = deepcopy(frozen)
    reused = migrate_acceptance_checkpoint(baseline, changed, frozen, baseline.ref().model_dump(mode="json"))
    assert set(reused) == {"A", "D", "F"}
    assert all(reused[task_id] == frozen[task_id] for task_id in reused)
    assert frozen == snapshot


def test_ancestor_set_changes_and_version_only_changes_prevent_reuse(forked_plan):
    baseline, frozen = forked_plan
    tasks = [task_with(task) if task.task_id == "B" else task for task in baseline.tasks]
    changed = plan_with(baseline, tasks)
    assert set(migrate_acceptance_checkpoint(baseline, changed, frozen, baseline.ref().model_dump(mode="json"))) == {"A", "D", "F"}
    removed = plan_with(baseline, [task for task in baseline.tasks if task.task_id != "B"])
    assert set(migrate_acceptance_checkpoint(baseline, removed, frozen, baseline.ref().model_dump(mode="json"))) == {"A", "D", "F"}


@pytest.mark.parametrize("field", ["requirement", "design"])
def test_changed_requirement_or_design_ref_and_wrong_binding_reuse_nothing(forked_plan, field):
    baseline, frozen = forked_plan
    previous = getattr(baseline, field)
    changed_ref = Ref(object_id=previous.object_id, version=previous.version + 1, content_hash=previous.content_hash)
    changed = baseline.model_copy(update={field: changed_ref})
    assert migrate_acceptance_checkpoint(baseline, changed, frozen, baseline.ref().model_dump(mode="json")) == {}
    assert migrate_acceptance_checkpoint(baseline, baseline, frozen, {}) == {}


@pytest.mark.parametrize("bad_policy", [False, True])
def test_bad_frozen_hash_or_current_policy_is_rejected_without_mutating_source(forked_plan, bad_policy):
    baseline, frozen = forked_plan
    frozen = deepcopy(frozen)
    task = baseline.tasks[0]
    if bad_policy:
        frozen[task.task_id] = FrozenAcceptance.freeze(task, {task.checks[0]: "import re\nassert 1 + 1 == 2\n"}).model_dump(mode="json")
    else:
        frozen[task.task_id]["checks"][task.checks[0]] = "assert 2 + 2 == 4\n"
    snapshot = deepcopy(frozen)
    if bad_policy:
        reused = migrate_acceptance_checkpoint(baseline, baseline, frozen, baseline.ref().model_dump(mode="json"))
        assert task.task_id not in reused
        assert set(reused) == set(frozen) - {task.task_id}
    else:
        with pytest.raises(OutputSchemaError, match="cannot be safely reused"):
            migrate_acceptance_checkpoint(baseline, baseline, frozen, baseline.ref().model_dump(mode="json"))
    assert frozen == snapshot


def test_direct_leaf_revision_reuses_seven_frozen_tasks_but_still_gets_a_fresh_full_review(tmp_path, bundle):
    provider = DirectRevisionProvider(bundle)

    def factory(ledger):
        return AgentRunner(provider, ledger=ledger)

    with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"), runner_factory=factory)) as client:
        original = prepared(client, bundle)
        tasks = deepcopy(original["plan"]["tasks"])
        tasks[-1]["goal"] += "，修订文档检查"
        count = len(provider.requests)
        response = client.post(f"/api/workflows/{original['workflow_id']}/plan-revisions", json={
            "tasks": tasks, "expected_plan_ref": {key: original["plan"][key]
                        for key in ("schema_version", "object_id", "version", "content_hash")},
            "operation_id": "targeted-leaf-revision",
        })
        assert response.status_code == 200, response.text
        revised = wait_for_stage(client, original["workflow_id"], "plan_pending_approval")
        assert [request.task_kind for request in provider.requests[count:]] == ["plan_review", "acceptance_definition"]
        assert provider.requests[-1].task_id == tasks[-1]["task_id"]
        for task in tasks[:-1]:
            assert revised["acceptance"][task["task_id"]] == original["acceptance"][task["task_id"]]
        assert len(revised["acceptance_migration"]["reused_tasks"]) == 7
        assert revised["acceptance_history"][-1]["acceptance"] == original["acceptance"]
        assert revised["pending_plan_revision"]["baseline_plan"] == original["plan"]
        assert revised["snapshot"]["tasks"] == [] and "plan" not in revised["approvals"]


def test_report_manifest_includes_complete_print_sources_and_readonly_container_semantics(bundle):
    producer = task_with(bundle.plan.tasks[5], write_scope=["reports/concurrency"], checks=["check:rounds"])
    consumer = task_with(bundle.plan.tasks[7], write_scope=["reports/summary"])
    source = "for round_number in range(5):\n    print('Round', round_number, 'ok', 10, 'insufficient', 90)\nassert len(range(5)) == 5\n"
    frozen = FrozenAcceptance.freeze(producer, {producer.checks[0]: source}).model_dump(mode="json")
    manifest = _task_manifest(bundle.requirement, bundle.design, consumer, [producer], frozen_inputs={producer.task_id: frozen})
    evidence = manifest["upstream_frozen_output_producers"]
    assert evidence == [{"task_id": producer.task_id, "check_id": "check:rounds",
                         "report_path": "reports/concurrency/check-report.json", "source": source}]
    assert "not dict instances" in manifest["source_metadata_schema"]["file_fields"]["data"]
    assert "not list instances" in manifest["source_metadata_schema"]["file_fields"]["data"]
    assert "unittest.TestResult()" in manifest["prompt"]
    validate_check("report = source_metadata['reports/concurrency/check-report.json']['data']\n"
                   "assert 'checks' in report\nassert len(report['checks']) > 0\n"
                   "assert all(check['exit_code'] == 0 for check in report['checks'])\n", [])


@pytest.mark.parametrize("changed", [False, True], ids=["unchanged-regenerates", "changed-can-repair"])
def test_historical_invalid_script_does_not_block_revision_and_is_regenerated(tmp_path, bundle, changed):
    provider = DirectRevisionProvider(bundle)
    store = Store(tmp_path / "state.sqlite3")
    with TestClient(create_app(store=store, runner_factory=lambda ledger: AgentRunner(provider, ledger=ledger))) as client:
        original = prepared(client, bundle)
        task = bundle.plan.tasks[-1]
        old_script = "assert len([]) == 1 or True\n"
        old_frozen = FrozenAcceptance.freeze(task, {check: old_script for check in task.checks}).model_dump(mode="json")
        # Simulate an intact checkpoint frozen under an earlier validator version.
        with store.connection() as db:
            document = json.loads(db.execute("SELECT body FROM workflow_documents WHERE id=?",
                                            (original["workflow_id"],)).fetchone()[0])
            document["acceptance"][task.task_id] = old_frozen
            db.execute("UPDATE workflow_documents SET body=? WHERE id=?",
                       (json.dumps(document), original["workflow_id"]))
            db.commit()
        rows = deepcopy(original["plan"]["tasks"])
        if changed:
            rows[-1]["checks"] = ["check:corrected-document-content"]
        count = len(provider.requests)
        reply = client.post(f"/api/workflows/{original['workflow_id']}/plan-revisions", json={
            "tasks": rows, "expected_plan_ref": {key: original["plan"][key]
                for key in ("schema_version", "object_id", "version", "content_hash")},
            "operation_id": f"repair-historical-check-{changed}",
        })
        assert reply.status_code == 200, reply.text
        revised = wait_for_stage(client, original["workflow_id"], "plan_pending_approval")
        assert [request.task_kind for request in provider.requests[count:]] == ["plan_review", "acceptance_definition"]
        assert provider.requests[-1].task_id == task.task_id
        assert len(revised["acceptance_migration"]["reused_tasks"]) == 7
        assert revised["pending_plan_revision"]["checkpoint_acceptance"][task.task_id] == old_frozen
        assert revised["acceptance_history"][-1]["acceptance"][task.task_id] == old_frozen
        assert all(source != old_script for source in revised["acceptance"][task.task_id]["checks"].values())
        for source in revised["acceptance"][task.task_id]["checks"].values():
            validate_check(source, [])


@pytest.mark.parametrize("removed", [False, True], ids=["changed-task", "removed-task"])
def test_tampered_historical_check_is_rejected_even_when_task_is_not_eligible_for_reuse(forked_plan, removed):
    baseline, frozen = forked_plan
    task = baseline.tasks[0]
    rows = [item for item in baseline.tasks if item != task] if removed else [
        task_with(item, goal="Repair changed task") if item == task else item for item in baseline.tasks]
    revised = plan_with(baseline, rows)
    frozen = deepcopy(frozen)
    frozen[task.task_id]["checks"][task.checks[0]] += "# changed after freeze\n"
    with pytest.raises(OutputSchemaError, match="cannot be safely reused"):
        migrate_acceptance_checkpoint(baseline, revised, frozen, baseline.ref().model_dump(mode="json"))

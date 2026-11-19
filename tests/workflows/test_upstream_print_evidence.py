"""Direct report output producers must survive bounded ancestor source selection."""

from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.workflows.acceptance import _task_manifest, _upstream_print_evidence

from test_acceptance_corrections import task_with


def producer(bundle, identifier, dependencies=(), checks=None):
    return task_with(bundle.plan.tasks[5], task_id=identifier, type="integrate", executor="Momus",
                     write_scope=[f"reports/{identifier}"], outputs=[f"artifact:{identifier}"],
                     inputs=[f"artifact:{parent}" for parent in dependencies], depends_on=list(dependencies),
                     checks=checks or [f"check:{identifier}"])


def freeze(task):
    sources = {check: f"print({check!r}, 'full_round_0_testsRun', 8)\nassert len(range(8)) == 8\n"
               for check in task.checks}
    return FrozenAcceptance.freeze(task, sources).model_dump(mode="json")


def test_late_direct_report_sources_precede_twelve_earlier_printing_ancestors_without_truncation(bundle):
    early = [producer(bundle, f"FR{index}", [f"FR{index - 1}"] if index else []) for index in range(12)]
    direct = producer(bundle, "INTG", [early[-1].task_id], ["full-round-counts", "predecessor-report-validated"])
    consumer = task_with(bundle.plan.tasks[7], task_id="DOC", type="doc", inputs=direct.outputs,
                         depends_on=[direct.task_id])
    frozen = {item.task_id: freeze(item) for item in [*early, direct]}
    manifest = _task_manifest(bundle.requirement, bundle.design, consumer, [*early, direct], frozen_inputs=frozen)
    evidence = manifest["upstream_frozen_output_producers"]
    assert len(evidence) == 12
    assert [(row["task_id"], row["check_id"]) for row in evidence[:2]] == [
        (direct.task_id, check) for check in direct.checks]
    for row in evidence[:2]:
        assert row["report_path"] == "reports/INTG/check-report.json"
        assert row["source"] == frozen[direct.task_id]["checks"][row["check_id"]]
    # After the two direct sources, shortest dependency distance wins over plan order.
    assert evidence[2]["task_id"] == "FR11" and evidence[-1]["task_id"] == "FR2"
    assert "Each check's output format is independent" in manifest["prompt"]
    assert "exact report_path and check_id" in manifest["prompt"]


def test_actual_physical_report_input_is_direct_even_when_output_id_is_logical(bundle):
    far = producer(bundle, "FAR")
    direct = producer(bundle, "DIRECT", [far.task_id])
    closer_other = producer(bundle, "OTHER")
    consumer = task_with(bundle.plan.tasks[7], inputs=["reports/DIRECT/check-report.json"],
                         depends_on=[closer_other.task_id, direct.task_id])
    frozen = {item.task_id: freeze(item) for item in [far, closer_other, direct]}
    evidence = _upstream_print_evidence(consumer, [far, closer_other, direct], frozen)
    assert [row["task_id"] for row in evidence] == ["DIRECT", "OTHER", "FAR"]


def test_source_budget_keeps_complete_direct_source_and_never_truncates_later_sources(bundle):
    early = producer(bundle, "EARLY")
    direct = producer(bundle, "DIRECT", [early.task_id])
    consumer = task_with(bundle.plan.tasks[7], inputs=direct.outputs, depends_on=[direct.task_id])
    source = "# " + "x" * 65000 + "\nprint('direct-count', 8)\nassert len(range(8)) == 8\n"
    old_source = "# " + "x" * 1000 + "\nprint('old-count', 4)\nassert len(range(4)) == 4\n"
    frozen = {direct.task_id: FrozenAcceptance.freeze(direct, {direct.checks[0]: source}).model_dump(mode="json"),
              early.task_id: FrozenAcceptance.freeze(early, {early.checks[0]: old_source}).model_dump(mode="json")}
    evidence = _upstream_print_evidence(consumer, [early, direct], frozen)
    assert len(evidence) == 1 and evidence[0]["source"] == source

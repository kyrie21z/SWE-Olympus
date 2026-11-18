"""A declared README gets a real author without sacrificing its independent checks."""

import pytest

from olympus.agents.argus import ArgusReviewer
from olympus.agents.hephaestus import HephaestusImplementer
from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.agents.runtime import AgentRunner, MockProvider, OutputSchemaError
from olympus.contracts.models import Run, TaskRuntime
from olympus.execution import ArtifactAcceptanceService, LocalPythonExecutor
from olympus.execution.profile import execution_profile_issues
from olympus.orchestrator.scheduler import Scheduler
from olympus.orchestrator.state_machine import RunContext
from olympus.planning.service import PlanService
from olympus.planning.validator import ancestor_map, find_dependency_cycles, validate_plan_semantics
from olympus.workflows.acceptance import _input_tasks
from olympus.workflows.document_normalization import NORMALIZATION_SOURCE, normalize_document_authorship
from olympus.workflows.service import LocalPlanner

from test_acceptance_corrections import plan_with, task_with


def document_plan(bundle, *, filename="docs/README.md", self_input=False):
    base = bundle.plan.tasks[0]
    sources = [item["id"] for item in bundle.requirement.content["requirements"]] + [bundle.design.content["elements"][0]["id"]]
    doc = task_with(base, task_id="T-DOC-README", type="doc", executor="Hermes", reviewer="Argus",
                    goal="Author README Chinese search and document version run instructions",
                    sources=sources, depends_on=["T07"], inputs=["artifact:T07"] + ([filename] if self_input else []),
                    outputs=[filename], write_scope=["docs"], checks=["README contains Chinese usage instructions", "README explains how to run checks"])
    downstream = task_with(doc, task_id="T-VERIFY-PACKAGE", type="integrate", depends_on=[doc.task_id],
                           inputs=[filename], outputs=["artifact:package"], write_scope=["reports/package"])
    return plan_with(bundle.plan, [*bundle.plan.tasks, doc, downstream]), doc, downstream


def test_real_readonly_readme_candidate_gets_exact_author_and_independent_checker_with_downstream_input_preserved(bundle):
    original, before, downstream = document_plan(bundle, self_input=True)
    result = normalize_document_authorship(original, bundle.requirement, bundle.design)
    writer = next(task for task in result.tasks if task.task_id.startswith("DOC-AUTHOR-"))
    checker = next(task for task in result.tasks if task.task_id == before.task_id)
    assert writer.executor == "Hephaestus" and writer.type == "implement" and writer.reviewer == "Argus"
    assert writer.write_scope == writer.outputs == ["docs/README.md"]
    assert writer.inputs == ["artifact:T07"] and writer.depends_on == before.depends_on
    assert writer.sources == before.sources and writer.checks == before.checks and writer.policy == before.policy
    assert writer.required == checker.required == before.required
    assert writer.goal == before.goal and checker.goal.startswith("Verify")
    assert checker.executor == "Hermes" and checker.type == "doc" and checker.reviewer == before.reviewer
    assert checker.checks == before.checks and checker.sources == before.sources
    assert all(dependency in checker.depends_on for dependency in before.depends_on)
    assert writer.task_id in checker.depends_on and "docs/README.md" in checker.inputs
    assert checker.outputs == [checker.write_scope[0] + "/check-report.json"]
    package = next(task for task in result.tasks if task.task_id == downstream.task_id)
    assert package == downstream and package.inputs == ["docs/README.md"]
    assert writer in _input_tasks(result, package)
    assert writer.task_id in ancestor_map(result)[package.task_id]
    assert find_dependency_cycles(result) == []
    assert validate_plan_semantics(result, bundle.requirement, bundle.design).valid
    assert execution_profile_issues(result) == []
    assert original.tasks[-2] == before  # Canonical proposals are not mutated in place.
    assert result.provenance[:-1] == original.provenance
    assert result.provenance[-1].source_id == NORMALIZATION_SOURCE and result.provenance[-1].kind == "material"
    assert normalize_document_authorship(result, bundle.requirement, bundle.design) is result
    again = normalize_document_authorship(original, bundle.requirement, bundle.design)
    assert again == result


def test_local_planner_integrates_normalization_before_profile_and_versions_real_changed_tasks(bundle):
    proposal, _, _ = document_plan(bundle)
    result = LocalPlanner._materialize({"tasks": [task.semantic_content() for task in proposal.tasks]},
                                       bundle.requirement, bundle.design, "real_model")
    assert len(result.tasks) == len(proposal.tasks) + 1
    revised = LocalPlanner.version_revision(proposal, result)
    assert revised.version == proposal.version + 1
    assert next(task for task in revised.tasks if task.task_id == "T-DOC-README").version == proposal.tasks[-2].version + 1
    assert any(item.source_id == NORMALIZATION_SOURCE for item in revised.provenance)
    assert execution_profile_issues(result) == []


@pytest.mark.parametrize("reachable", [False, True])
def test_unique_existing_compatible_author_is_connected_or_left_unchanged(bundle, reachable):
    original, doc, _ = document_plan(bundle)
    writer = task_with(doc, task_id="EXISTING-DOC-AUTHOR", type="implement", executor="Hephaestus",
                       write_scope=["docs/README.md"], outputs=["docs/README.md"])
    tasks = [*original.tasks, writer]
    if reachable:
        connected = task_with(doc, depends_on=[*doc.depends_on, writer.task_id], inputs=[*doc.inputs, "docs/README.md"])
        tasks = [connected if task.task_id == doc.task_id else task for task in tasks]
    original = plan_with(original, tasks)
    result = normalize_document_authorship(original, bundle.requirement, bundle.design)
    assert len(result.tasks) == len(original.tasks)
    assert next(task for task in result.tasks if task.task_id == writer.task_id) == writer
    if reachable:
        assert result is original
    else:
        checker = next(task for task in result.tasks if task.task_id == doc.task_id)
        assert writer.task_id in checker.depends_on and "docs/README.md" in checker.inputs


@pytest.mark.parametrize("problem", ["parallel_authors", "cycle_author", "unsafe", "unsafe_drive", "unsupported", "task_limit"])
def test_unsafe_or_ambiguous_changes_are_rejected_without_guessing_or_dropping_checks(bundle, problem):
    filename = "../README.md" if problem == "unsafe" else "C:\\README.md" if problem == "unsafe_drive" else "docs/README.md"
    original, doc, _ = document_plan(bundle, filename=filename)
    tasks = list(original.tasks)
    if problem in {"parallel_authors", "cycle_author"}:
        for number in range(2 if problem == "parallel_authors" else 1):
            tasks.append(task_with(doc, task_id=f"AUTHOR-{number}", type="implement", executor="Hephaestus",
                                   write_scope=["docs/README.md"], outputs=["docs/README.md"],
                                   depends_on=[doc.task_id] if problem == "cycle_author" else ["T07"]))
    elif problem == "unsupported":
        tasks = [task_with(task, outputs=["docs/README.md", "docs/test_code.py"]) if task.task_id == doc.task_id else task
                 for task in tasks]
    elif problem == "task_limit":
        while len(tasks) < 30:
            tasks.append(task_with(bundle.plan.tasks[0], task_id=f"EXTRA-{len(tasks)}", depends_on=[], inputs=[],
                                   outputs=[f"artifact:EXTRA-{len(tasks)}"]))
    proposal = plan_with(original, tasks)
    with pytest.raises(OutputSchemaError):
        normalize_document_authorship(proposal, bundle.requirement, bundle.design)
    assert next(task for task in proposal.tasks if task.task_id == doc.task_id).checks == doc.checks


def test_python_or_stub_claims_are_not_normalized_into_documents(bundle):
    for filename in ("docs/test_module.py", "docs/interface.pyi"):
        proposal, _, _ = document_plan(bundle, filename=filename)
        with pytest.raises(OutputSchemaError):
            LocalPlanner._materialize({"tasks": [task.semantic_content() for task in proposal.tasks]},
                                      bundle.requirement, bundle.design, "real_model")


def test_multiple_document_outputs_accumulate_on_one_existing_author(bundle):
    original, doc, _ = document_plan(bundle)
    doc = task_with(doc, outputs=["docs/README.md", "docs/guide.txt"])
    writer = task_with(doc, task_id="EXISTING-AUTHOR", type="implement", executor="Hephaestus",
                       write_scope=["docs/README.md", "docs/guide.txt"], outputs=["artifact:AUTHOR-DOCS"])
    original = plan_with(original, [doc if task.task_id == doc.task_id else task for task in original.tasks] + [writer])
    result = normalize_document_authorship(original, bundle.requirement, bundle.design)
    author = next(task for task in result.tasks if task.task_id == writer.task_id)
    assert author.outputs == ["artifact:AUTHOR-DOCS", "docs/README.md", "docs/guide.txt"]
    checker = next(task for task in result.tasks if task.task_id == doc.task_id)
    assert all(document in checker.inputs for document in ("docs/README.md", "docs/guide.txt"))
    assert author in _input_tasks(result, checker)
    assert execution_profile_issues(result) == []


def test_already_reachable_parallel_authors_are_rejected_instead_of_treated_as_valid_noop(bundle):
    original, doc, _ = document_plan(bundle)
    writers = [task_with(doc, task_id=f"WRITER-{number}", type="implement", executor="Hephaestus",
                         write_scope=["docs/README.md"], outputs=[f"artifact:WRITER-{number}"],
                         depends_on=["T07"], inputs=["artifact:T07"]) for number in range(2)]
    connected = task_with(doc, depends_on=[*doc.depends_on, *[writer.task_id for writer in writers]],
                          inputs=[*doc.inputs, *[writer.outputs[0] for writer in writers]])
    proposal = plan_with(original, [connected if task.task_id == doc.task_id else task for task in original.tasks] + writers)
    with pytest.raises(OutputSchemaError, match="normalization rejected"):
        normalize_document_authorship(proposal, bundle.requirement, bundle.design)


def test_original_report_output_remains_consumable_by_real_downstream_execution(bundle, tmp_path):
    sources = [item["id"] for item in bundle.requirement.content["requirements"]] + [bundle.design.content["elements"][0]["id"]]
    base = task_with(bundle.plan.tasks[0], task_id="BASE", sources=sources, depends_on=[], inputs=[],
                     outputs=["artifact:BASE"], write_scope=["base.py"], checks=["check:base"])
    doc = task_with(base, task_id="T-DOC-README", type="doc", executor="Hermes",
                    goal="Author README Chinese run instructions", depends_on=["BASE"], inputs=["artifact:BASE"],
                    write_scope=["docs"], outputs=["docs/README.md", "docs/check-report.json"], checks=["check:doc"])
    downstream = task_with(doc, task_id="READ-REPORT", type="test", executor="Momus", depends_on=[doc.task_id],
                           inputs=["docs/check-report.json"], outputs=["artifact:READ"],
                           write_scope=["reports/downstream"], checks=["check:report"])
    plan = normalize_document_authorship(plan_with(bundle.plan, [base, doc, downstream]), bundle.requirement, bundle.design)
    checked_doc = next(task for task in plan.tasks if task.task_id == doc.task_id)
    assert checked_doc.write_scope == ["docs"] and checked_doc.outputs == ["docs/check-report.json"]
    package = PlanService().prepare(bundle.requirement, bundle.design, plan)
    run = Run(run_id="RUN-DOC-REPORT", mode="real_tool", status="active", call_budget=60,
              approved_package=[bundle.requirement.ref(), bundle.design.ref(), plan.ref()],
              tasks=[TaskRuntime(task_id=task.task_id) for task in plan.tasks])
    scheduler = Scheduler(plan, package.manifest, RunContext(run=run))
    author = next(task for task in plan.tasks if task.task_id.startswith("DOC-AUTHOR-"))
    runner = AgentRunner(MockProvider(fixtures={
        "Hephaestus:BASE": {"output": {"changes": [{"path": "base.py", "content": "VALUE = 1\n"}]}},
        f"Hephaestus:{author.task_id}": {"output": {"changes": [{"path": "docs/README.md", "content": "中文运行说明\n"}]}},
        "Argus:artifact_review": {"output": {"verdict": "passed", "findings": []}},
    }))
    executor = LocalPythonExecutor(scheduler, tmp_path / "executor", HephaestusImplementer(runner))
    acceptance = ArtifactAcceptanceService(executor, ArgusReviewer(runner))
    for task in plan.tasks:
        if task.task_id == "BASE":
            source = "assert source_metadata['base.py']['kind'] == 'python'\n"
        elif task.task_id == downstream.task_id:
            source = "report = source_metadata['docs/check-report.json']['data']\n"
            source += "assert report['task'] == 'T-DOC-README'\nassert len(report['checks']) == 1\n"
            source += "assert all(check['exit_code'] == 0 for check in report['checks'])\n"
        else:
            source = "assert '中文' in source_metadata['docs/README.md']['text']\n"
        executor.register_task(task.task_id, FrozenAcceptance.freeze(task, {check: source for check in task.checks}))
    for task in plan.tasks:
        scheduler.reconcile()
        assert scheduler.dispatch(task.task_id, executor) is executor
        receipt = executor.wait(scheduler.attempt_for(task.task_id))
        assert receipt.state == "succeeded", receipt.error
        assert acceptance.collect(task.task_id, receipt.attempt_id)[0] == "accepted"
    assert scheduler.ctx.state_of(downstream.task_id) == "accepted"

"""REQ -> DS -> Task traceability index.

The index is a planning view: rows report ``planned`` or ``missing_coverage``,
verification stays ``unverified`` and ``accepted`` is pinned to ``False``. It is
also the data source for the frontend traceability matrix.
"""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from olympus.contracts.models import (
    Contract,
    Design,
    Hash,
    Id,
    Plan,
    Ref,
    Requirement,
    fingerprint,
)
from olympus.planning.validator import (
    design_element_ids,
    requirement_ids,
    scan_out_of_scope,
    uncovered_design_elements,
)


class TraceabilityRow(Contract):
    req_id: Id
    req_text: Id
    ds_ids: list[Id]
    interface_tasks: list[Id]
    implement_tasks: list[Id]
    test_tasks: list[Id]
    integrate_tasks: list[Id]
    doc_tasks: list[Id]
    all_tasks: list[Id]
    stage_status: Literal["planned", "missing_coverage"]
    verification_status: Literal["unverified"] = "unverified"
    accepted: Literal[False] = False


class PlanningTraceabilityIndex(Contract):
    index_id: Id
    req_ref: Ref
    ds_ref: Ref
    plan_ref: Ref
    rows: list[TraceabilityRow]
    design_to_tasks: dict[Id, list[Id]]
    task_to_sources: dict[Id, list[Id]]
    uncovered_requirements: list[Id]
    uncovered_design_elements: list[Id]
    missing_test_requirements: list[Id]
    out_of_scope_hits: list[dict[str, Id]]
    dangling_sources: list[dict[str, Id]]
    coverage_complete: bool
    index_hash: Hash

    @model_validator(mode="after")
    def self_consistent(self):
        if self.index_hash != index_digest(self):
            raise ValueError("index_hash does not match canonical semantic fields")
        return self


def index_digest(index: PlanningTraceabilityIndex) -> str:
    payload = index.model_dump(mode="json", exclude={"schema_version", "index_hash"})
    return fingerprint(payload)


def _task_ids_by_type(tasks, kind: str) -> list[str]:
    return [task.task_id for task in tasks if task.type == kind]


def build_planning_traceability(req: Requirement, ds: Design, plan: Plan) -> PlanningTraceabilityIndex:
    if plan.requirement != req.ref():
        raise ValueError("plan does not bind the supplied requirement version")
    if plan.design != ds.ref():
        raise ValueError("plan does not bind the supplied design version")
    if ds.requirement != req.ref():
        raise ValueError("design does not bind the supplied requirement version")

    req_ids = requirement_ids(req)
    ds_ids = design_element_ids(ds)
    elements = ds.content["elements"]
    known_sources = set(req_ids) | set(ds_ids)

    rows: list[TraceabilityRow] = []
    for item in req.content["requirements"]:
        req_id = item["id"]
        linked_ds = [element["id"] for element in elements if req_id in element["sources"]]
        tasks = [task for task in plan.tasks if req_id in task.sources]
        all_tasks = [task.task_id for task in tasks]
        rows.append(
            TraceabilityRow(
                req_id=req_id,
                req_text=item["text"],
                ds_ids=linked_ds,
                interface_tasks=_task_ids_by_type(tasks, "interface"),
                implement_tasks=_task_ids_by_type(tasks, "implement"),
                test_tasks=_task_ids_by_type(tasks, "test"),
                integrate_tasks=_task_ids_by_type(tasks, "integrate"),
                doc_tasks=_task_ids_by_type(tasks, "doc"),
                all_tasks=all_tasks,
                stage_status="planned" if all_tasks else "missing_coverage",
            )
        )

    missing_test = [row.req_id for row in rows if not row.test_tasks]
    dangling = [
        {"task_id": task.task_id, "source_id": source}
        for task in plan.tasks
        for source in task.sources
        if source not in known_sources
    ]
    hits = scan_out_of_scope(req, plan.tasks)
    uncovered_requirements = [row.req_id for row in rows if not row.all_tasks]
    uncovered_design = uncovered_design_elements(plan, req, ds)

    fields = {
        "index_id": f"TRACE-{req.object_id[:60]}-{ds.object_id[:40]}-{plan.object_id[:40]}",
        "req_ref": req.ref(),
        "ds_ref": ds.ref(),
        "plan_ref": plan.ref(),
        "rows": rows,
        "design_to_tasks": {
            ds_id: [task.task_id for task in plan.tasks if ds_id in task.sources] for ds_id in ds_ids
        },
        "task_to_sources": {task.task_id: list(task.sources) for task in plan.tasks},
        "uncovered_requirements": uncovered_requirements,
        "uncovered_design_elements": uncovered_design,
        "missing_test_requirements": missing_test,
        "out_of_scope_hits": hits,
        "dangling_sources": dangling,
        "coverage_complete": not (
            uncovered_requirements or uncovered_design or missing_test or hits or dangling
        ),
    }
    draft = PlanningTraceabilityIndex.model_construct(**fields, schema_version="1.0", index_hash="0" * 64)
    return PlanningTraceabilityIndex(**fields, index_hash=index_digest(draft))

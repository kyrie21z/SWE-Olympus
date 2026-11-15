"""Planning service that assembles a reviewable plan package.

The service validates and compiles a plan into a package for human and Momus
review. It deliberately carries no approval: ``approved`` and
``execution_started`` are pinned to ``False`` and no task state is ever written.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from olympus.contracts.models import Contract, Design, Id, Plan, Ref, Requirement
from olympus.planning.compiler import CompiledManifest, compile_plan
from olympus.planning.models import TaskCard, build_task_cards
from olympus.planning.validator import PlanValidationReport, validate_plan_semantics


class PlanRejectedError(ValueError):
    """Raised when a plan fails programmatic validation and cannot be packaged."""

    def __init__(self, report: PlanValidationReport):
        self.report = report
        codes = ", ".join(issue.code for issue in report.issues)
        super().__init__(f"plan rejected by semantic validation: {codes}")


class PlanPackage(Contract):
    package_id: Id
    req_ref: Ref
    ds_ref: Ref
    plan_ref: Ref
    task_cards: list[TaskCard] = Field(min_length=1)
    validation: PlanValidationReport
    manifest: CompiledManifest
    approved: Literal[False] = False
    execution_started: Literal[False] = False


class PlanService:
    """Validates and compiles plans without owning approval or execution."""

    def prepare(self, req: Requirement, ds: Design, plan: Plan) -> PlanPackage:
        report = validate_plan_semantics(plan, req, ds)
        if not report.valid:
            raise PlanRejectedError(report)
        manifest = compile_plan(plan, req, ds)
        return PlanPackage(
            package_id=f"PKG-{plan.object_id[:120]}-V{plan.version}",
            req_ref=req.ref(),
            ds_ref=ds.ref(),
            plan_ref=plan.ref(),
            task_cards=build_task_cards(plan, req, ds),
            validation=report,
            manifest=manifest,
        )

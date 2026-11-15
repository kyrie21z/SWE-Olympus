"""Momus plan review and the single-revision planning workflow.

Momus runs programmatic validation plus its own semantic review, records a
``Review`` bound to the exact plan version, and never lets Daedalus close its
own blockers. The workflow allows at most one content revision and then halts
for a human.
"""

from __future__ import annotations

from typing import Iterable, Literal

from olympus.contracts.models import (
    Contract,
    Design,
    Id,
    Mode,
    NonNegative,
    Plan,
    Ref,
    Requirement,
    Review,
)
from olympus.planning.validator import (
    PlanValidationReport,
    requirement_ids,
    scan_out_of_scope,
    validate_plan_semantics,
)

DAEDALUS = "Daedalus"
MOMUS = "Momus"


class SemanticFinding(Contract):
    code: Id
    severity: Literal["blocker", "important"]
    task_id: str | None = None
    message: str


class PlanReview(Contract):
    review: Review
    validation: PlanValidationReport
    findings: list[SemanticFinding]
    revision_recommended: bool


class PlanningOutcome(Contract):
    status: Literal["passed", "blocked_awaiting_human"]
    req_ref: Ref
    ds_ref: Ref
    plan: Plan
    review: Review
    validation: PlanValidationReport
    reviews: list[Review]
    revisions_used: NonNegative
    halted_reason: Id | None = None
    findings: list[str]


class MomusPlanReviewer:
    reviewer_id = MOMUS

    def __init__(self, authorized_closers: Iterable[Id] = (), mode: Mode = "mock"):
        # Daedalus can never be an authorized closer of its own review blockers.
        self.authorized_closers = frozenset(authorized_closers) - {DAEDALUS}
        self.mode = mode

    def _granularity_findings(self, plan: Plan) -> list[SemanticFinding]:
        implementation = [task for task in plan.tasks if task.type == "implement"]
        findings: list[SemanticFinding] = []
        if len(implementation) == 1:
            covered = sorted({source for source in implementation[0].sources if source.startswith("FR-")})
            if len(covered) >= 3:
                findings.append(
                    SemanticFinding(
                        code="TASK_GRANULARITY",
                        severity="blocker",
                        task_id=implementation[0].task_id,
                        message=(
                            f"任务 {implementation[0].task_id} 将 {len(covered)} 条需求合并为一个实现任务，"
                            "无法独立验收，需拆分"
                        ),
                    )
                )
        return findings

    def _trace_findings(self, req: Requirement, ds: Design) -> list[SemanticFinding]:
        req_ids = requirement_ids(req)
        elements = ds.content["elements"]
        known_req = set(req_ids)
        findings: list[SemanticFinding] = []
        for element in elements:
            missing = [source for source in element["sources"] if source not in known_req]
            if missing:
                findings.append(
                    SemanticFinding(
                        code="SOURCE_ALIGNMENT",
                        severity="blocker",
                        message=f"设计元素 {element['id']} 引用了不存在的需求 {', '.join(missing)}",
                    )
                )
        traced = {source for element in elements for source in element["sources"]}
        for req_id in req_ids:
            if req_id not in traced:
                findings.append(
                    SemanticFinding(
                        code="DS_TRACE_GAP",
                        severity="important",
                        message=f"需求 {req_id} 没有对应的设计元素来源",
                    )
                )
        return findings

    def review(self, plan: Plan, req: Requirement, ds: Design) -> PlanReview:
        report = validate_plan_semantics(plan, req, ds)
        findings = [
            SemanticFinding(
                code=issue.code,
                severity=issue.severity,
                task_id=issue.task_id,
                message=issue.message,
            )
            for issue in report.issues
        ]
        findings.extend(self._granularity_findings(plan))
        findings.extend(self._trace_findings(req, ds))
        program_pairs = {(issue.code, issue.task_id, issue.source_id) for issue in report.issues}
        for hit in scan_out_of_scope(req, plan.tasks):
            if ("OUT_OF_SCOPE_VIOLATION", hit["task_id"], hit["term"]) in program_pairs:
                continue
            findings.append(
                SemanticFinding(
                    code="OUT_OF_SCOPE_VIOLATION",
                    severity="blocker",
                    task_id=hit["task_id"],
                    message=f"任务 {hit['task_id']} 的 {hit['field']} 出现范围外内容 {hit['term']}",
                )
            )

        if any(finding.severity == "blocker" for finding in findings):
            verdict = "blocked"
        elif findings:
            verdict = "needs_revision"
        else:
            verdict = "passed"
        review = Review(
            review_id=f"REV-{plan.object_id[:100]}-V{plan.version}"[:160],
            target=plan.ref(),
            author=DAEDALUS,
            reviewer=self.reviewer_id,
            verdict=verdict,
            findings=[f"[{finding.severity}] {finding.code}: {finding.message}" for finding in findings],
            evidence=[
                f"validation:{report.plan_ref.object_id}:v{report.plan_ref.version}",
                "program:validate_plan_semantics",
                "momus:semantic-review",
            ],
            mode=self.mode,
        )
        return PlanReview(
            review=review,
            validation=report,
            findings=findings,
            revision_recommended=verdict != "passed",
        )

    def close_blockers(
        self,
        review: Review,
        plan: Plan,
        closer: Id,
        verdict: Literal["passed", "needs_revision"] = "passed",
        evidence: list[Id] | None = None,
    ) -> Review:
        """Close Momus blockers only for Momus or an authorized non-author."""
        if review.target != plan.ref():
            raise ValueError("审查记录与当前计划版本不对应（stale review）")
        if review.verdict == "passed":
            raise ValueError("审查已通过，没有需要关闭的阻塞项")
        if closer == review.author:
            raise PermissionError("作者不得关闭自己的审查阻塞项")
        if closer != self.reviewer_id and closer not in self.authorized_closers:
            raise PermissionError(f"{closer} 未被授权关闭 Momus 审查阻塞项")
        return Review(
            review_id=f"{review.review_id}-CLOSE-{closer}"[:160],
            target=plan.ref(),
            author=review.author,
            reviewer=closer,
            verdict=verdict,
            findings=list(review.findings),
            evidence=evidence or list(review.evidence),
            mode=review.mode,
        )


class PlanningWorkflow:
    """Daedalus plan -> Momus review -> at most one revision -> halt for a human."""

    def __init__(self, planner, reviewer: MomusPlanReviewer | None = None, max_revisions: int = 1):
        self.planner = planner
        self.reviewer = reviewer or MomusPlanReviewer()
        if getattr(planner, "role_id", DAEDALUS) == self.reviewer.reviewer_id:
            raise ValueError("planner and reviewer must be different roles")
        self.max_revisions = self._revision_limit(max_revisions)

    @staticmethod
    def _revision_limit(limit: int) -> int:
        if not 0 <= limit <= 1:
            raise ValueError("单次规划最多允许一轮内容修订")
        return limit

    def run(
        self, req: Requirement, ds: Design, max_revisions: int | None = None
    ) -> PlanningOutcome:
        limit = self.max_revisions if max_revisions is None else self._revision_limit(max_revisions)
        plan = self.planner.plan(req, ds)
        reviews: list[Review] = []
        revisions_used = 0
        while True:
            result = self.reviewer.review(plan, req, ds)
            if result.review.target != plan.ref():
                raise ValueError("review target must bind the reviewed plan version")
            reviews.append(result.review)
            if result.review.verdict == "passed":
                return PlanningOutcome(
                    status="passed",
                    req_ref=req.ref(),
                    ds_ref=ds.ref(),
                    plan=plan,
                    review=result.review,
                    validation=result.validation,
                    reviews=reviews,
                    revisions_used=revisions_used,
                    findings=[],
                )
            if revisions_used >= limit:
                return PlanningOutcome(
                    status="blocked_awaiting_human",
                    req_ref=req.ref(),
                    ds_ref=ds.ref(),
                    plan=plan,
                    review=result.review,
                    validation=result.validation,
                    reviews=reviews,
                    revisions_used=revisions_used,
                    halted_reason="REVISION_LIMIT_REACHED",
                    findings=list(result.review.findings),
                )
            revised = self.planner.revise_plan(plan, result.review.findings, req, ds)
            if revised.version != plan.version + 1 or revised.ref() == plan.ref():
                raise ValueError("revision must bump the plan version and its content hash")
            plan = revised
            revisions_used += 1

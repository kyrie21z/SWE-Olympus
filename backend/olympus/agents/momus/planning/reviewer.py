"""Momus plan review and the single-revision planning workflow.

Momus runs programmatic validation plus its own semantic review, records a
``Review`` bound to the exact plan version, and never lets Daedalus close its
own blockers. The workflow allows at most one content revision and then halts
for a human.
"""

from __future__ import annotations

from typing import Callable, Iterable, Literal

from pydantic import BaseModel, ConfigDict

from olympus.agents.momus.design.reviewer import review_output_schema

from olympus.agents.runtime import AgentRunner, OutputSchemaError, RolePermissions, RoleRunInput
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
    fingerprint,
)
from olympus.planning.validator import (
    PlanValidationReport,
    requirement_ids,
    scan_out_of_scope,
    validate_plan_semantics,
)
from olympus.reviews.common import CommonReviewer, ReviewFinding, ReviewRecord, ReviewSubmission

DAEDALUS = "Daedalus"
MOMUS = "Momus"
REVIEW_TASK_KIND = "plan_review"
RECHECK_TASK_KIND = "plan_finding_check"
PLAN_REVIEW_CHECKS = ["program:validate_plan_semantics", "momus:semantic-review"]


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
    record: ReviewRecord


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


class TaskCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: Id
    field: Literal["goal", "type", "sources", "inputs", "outputs", "depends_on", "checks",
                   "write_scope", "executor", "reviewer", "resources"]
    quote: Id


class FindingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: Id
    decision: Literal["keep", "resolved"]
    citations: list[TaskCitation]


class PlanFindingChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[FindingDecision]


class MomusPlanReviewer:
    reviewer_id = MOMUS

    def __init__(
        self,
        authorized_closers: Iterable[Id] = (),
        mode: Mode = "mock",
        common_reviewer: CommonReviewer | None = None,
        runner: AgentRunner | None = None,
        program_checks: Callable[[Plan, Requirement, Design], Iterable[SemanticFinding]] | None = None,
        program_check_id: Id = "program:additional-plan-checks",
        review_context: dict | None = None,
    ):
        # Daedalus can never be an authorized closer of its own review blockers.
        self.authorized_closers = frozenset(authorized_closers) - {DAEDALUS}
        self.mode = mode
        self.common_reviewer = common_reviewer if common_reviewer is not None else CommonReviewer()
        self.runner = runner
        self.program_checks, self.program_check_id = program_checks, program_check_id
        self.review_context = review_context

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

    @staticmethod
    def _semantic_findings(output: dict, plan: Plan) -> list[SemanticFinding]:
        """Validate model observations; only program code owns the final verdict."""
        try:
            verdict = output.get("verdict")
            if verdict not in {"passed", "blocked", "needs_revision"}:
                raise ValueError("semantic review requires a valid verdict")
            if "target" in output and Ref.model_validate(output["target"]) != plan.ref():
                raise ValueError("semantic review target is stale")
            if "findings" not in output and "issues" not in output:
                raise ValueError("semantic review requires findings or issues")
            observations = []
            for key in ("findings", "issues"):
                items = output.get(key, [])
                if not isinstance(items, list):
                    raise ValueError(f"semantic review {key} must be a list")
                for item in items:
                    if isinstance(item, str):
                        item = {
                            "code": "MODEL_FINDING",
                            "severity": "blocker" if verdict == "blocked" else "important",
                            "message": item,
                        }
                    finding = SemanticFinding.model_validate(item)
                    if not finding.message.strip():
                        raise ValueError("semantic finding message must be non-empty")
                    if finding.task_id is not None and finding.task_id not in {t.task_id for t in plan.tasks}:
                        raise ValueError("semantic finding references an unknown task")
                    if finding not in observations:
                        observations.append(finding)
            if verdict != "passed" and not observations:
                raise ValueError("blocked/revision verdict must include actionable observations")
            return observations
        except (ValueError, TypeError) as exc:
            raise OutputSchemaError(f"invalid semantic review: {exc}") from exc

    def _run_agent(self, plan: Plan, req: Requirement, ds: Design):
        schema = review_output_schema(SemanticFinding)
        manifest = {
            "prompt": (
                "You are Momus, independent plan reviewer. Compare the full requirement, design and tasks; "
                "check meaning, omissions, coverage, granularity and out-of-scope work. "
                "Return JSON verdict and findings (code, severity=blocker/important, task_id or null, message). "
                "For each blocker identify an approved requirement or design obligation, the affected task, "
                "and how the current plan fails to produce or verify the required outcome. Use one finding per "
                "gap, check equivalent coverage in other tasks, and mark refinements important. "
                "Never close findings or grant human approval. Empty findings means no observed defect, "
                "not proof of implementation. Do not return private reasoning."
            ),
            "requirement": req.ref().model_dump(mode="json"),
            "requirement_content": req.content,
            "design": ds.ref().model_dump(mode="json"),
            "design_content": ds.content,
            "plan": plan.ref().model_dump(mode="json"),
            "tasks": [task.model_dump(mode="json") for task in plan.tasks],
            "output_schema": schema,
        }
        if self.review_context is not None:
            manifest["execution_context"] = self.review_context
            manifest["prompt"] += (
                " Evaluate deliverables against execution_context. Momus independently authors frozen "
                "executable checks stored by the harness; read-only tasks run those checks and emit real reports. "
                "Do not demand imaginary model-authored test modules from read-only tasks. Require a reachable "
                "author for documentation files, and real ancestor report inputs for report aggregation. "
                "The approved requirement and design define scope: do not add standalone files, extra artifacts "
                "or stricter delivery formats merely by preference. A complete README.md authored by a reachable "
                "Hephaestus implementation task and verified by a Hermes documentation task can satisfy approved "
                "Chinese run-instruction obligations; an additional standalone guide is required only if the "
                "approved inputs explicitly demand it. Verify that the README content covers the actual obligations. "
                "At execution time source_metadata[report_path]['data']['checks'][i]['output'] and exit_code "
                "come from real harness-executed checks. They are dynamic execution evidence, not interface "
                "declarations or invented future results. A plan that parses actual observed counts from these "
                "reports and compares them with requirement-backed expected values describes valid verification; "
                "still report fabricated counts, incompatible parsing or absent evidence when those defects genuinely occur. "
                "Before raising a missing-input finding, inspect the ENTIRE current task.inputs list and its "
                "reachable ancestor providers. Name the exact missing artifact and explain why existing listed "
                "inputs do not supply it; do not claim a source file is absent when it is explicitly listed and supplied. "
                "Interface contracts prove source/schema/signature facts; real business behavior belongs to "
                "the implementation and tests with executable inputs. Retain independent full review and report "
                "genuine approved-obligation blockers; these execution facts do not require a passed verdict. "
                "Return only concise actionable findings."
            )
        result = self.runner.run(
            RoleRunInput(
                role_id=MOMUS,
                task_kind=REVIEW_TASK_KIND,
                logical_task_id=f"LOG-REVIEW-{plan.object_id}-V{plan.version}",
                task_id=plan.object_id,
                input_manifest=manifest,
                input_refs=[req.ref(), ds.ref(), plan.ref()],
                output_schema=schema if self.runner.provider.mode == "real_model" else {},
                permissions=RolePermissions(
                    role_id=MOMUS,
                    allowed_task_kinds=[REVIEW_TASK_KIND],
                    allowed_input_keys=list(manifest),
                ),
                fixture_key="Momus:plan_review",
            ),
            output_validator=lambda output: self._semantic_findings(output, plan),
        )
        return self._semantic_findings(result.structured_output, plan), result

    def _recheck_agent(self, plan: Plan, req: Requirement, ds: Design, previous_review: ReviewRecord,
                       blockers: list[ReviewFinding]) -> str:
        schema = PlanFindingChecks.model_json_schema()
        manifest = {
            "prompt": (
                "Momus: verify only the listed findings against the revised tasks and approved inputs. "
                "Return one decision per finding_id. Use resolved only when the required outcome is covered, with "
                "verbatim evidence from current tasks. Cite the entire scalar field or one exact list entry. Coverage may move "
                "to a different task. Use keep for partially addressed or unverifiable findings, with citations []. "
                "Check equivalent wording and combine evidence across tasks. Do not add findings or stricter criteria."
            ),
            "plan_review_ref": previous_review.review.review_id,
            "plan": plan.ref().model_dump(mode="json"),
            "requirement_content": req.content,
            "design_content": ds.content,
            "tasks": [task.model_dump(mode="json") for task in plan.tasks],
            "blockers": [{"finding_id": item.finding_id, "severity": item.severity,
                          "message": item.message} for item in blockers],
            "output_schema": schema,
        }
        expected = {item.finding_id for item in blockers}
        tasks = {task.task_id: task for task in plan.tasks}

        def validate(output):
            try:
                result = PlanFindingChecks.model_validate(output)
                ids = [row.finding_id for row in result.decisions]
                if len(ids) != len(expected) or set(ids) != expected:
                    raise ValueError("one decision is required for every supplied blocker")
                for row in result.decisions:
                    if row.decision != "resolved":
                        continue
                    if not row.citations:
                        raise OutputSchemaError("resolved finding requires task evidence",
                                                hint="resolved 判断须引用当前任务中的完整字段或一个完整列表条目。")
                    for citation in row.citations:
                        task = tasks.get(citation.task_id)
                        field_value = getattr(task, citation.field, ())
                        matches = (citation.quote == field_value if isinstance(field_value, str)
                                   else citation.quote in field_value)
                        if task is None or not citation.quote.strip() or not matches:
                            raise OutputSchemaError("plan finding citation does not match current tasks",
                                                    hint="引用须使用当前任务的 task_id，并原样复制整个字段或一个完整列表条目；请勿截取或改写。")
            except OutputSchemaError:
                raise
            except ValueError as exc:
                raise OutputSchemaError("invalid plan finding verification",
                                        hint="请为首审清单中的每条意见返回一条复核判断，并保留 finding_id。") from exc
            return result

        result = self.runner.run(
            RoleRunInput(
                role_id=MOMUS, task_kind=RECHECK_TASK_KIND, task_id=plan.object_id,
                logical_task_id=f"LOG-RECHECK-{plan.object_id[:105]}-V{plan.version}-{plan.content_hash[:10]}",
                generation=plan.version, input_manifest=manifest,
                input_refs=[req.ref(), ds.ref(), previous_review.review.target, plan.ref()],
                output_schema=schema,
                permissions=RolePermissions(role_id=MOMUS, allowed_task_kinds=[RECHECK_TASK_KIND],
                                            allowed_input_keys=list(manifest)),
            ), output_validator=validate)
        by_id = {finding.finding_id: finding for finding in blockers}
        for row in validate(result.structured_output).decisions:
            if row.decision != "resolved" or not row.citations:
                continue
            citations = []
            for citation in row.citations:
                task = tasks.get(citation.task_id)
                field_value = getattr(task, citation.field, ())
                matches = citation.quote == field_value if isinstance(field_value, str) else citation.quote in field_value
                if not citation.quote.strip() or not matches:
                    break
                citations.append(f"{citation.task_id} · {citation.field}：{citation.quote}")
            if len(citations) == len(row.citations):
                finding = by_id[row.finding_id]
                finding.status, finding.closed_by = "closed", self.reviewer_id
                finding.closure_reason = "\n".join(citations)
        return result.execution_record_id

    def _program_findings(self, plan: Plan, req: Requirement, ds: Design):
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

        if self.program_checks is not None:
            findings.extend(self.program_checks(plan, req, ds))
        return report, findings

    def _required_checks(self, checks):
        return list(dict.fromkeys([*checks, *([self.program_check_id] if self.program_checks else [])]))

    @staticmethod
    def _passed_checks(common_findings, checks):
        program_blocked = any(f.raised_by == "program" and f.severity == "blocker" and f.status == "open"
                              for f in common_findings)
        semantic_blocked = any(f.raised_by == MOMUS and f.severity == "blocker" and f.status == "open"
                               for f in common_findings)
        return [check for check in checks
                if (check.startswith("program:") and not program_blocked)
                or (check.startswith("momus:") and not semantic_blocked)]

    def revalidate_program_checks(self, plan: Plan, req: Requirement, ds: Design,
                                  previous_review: ReviewRecord) -> PlanReview:
        """Refresh deterministic checks for identical content, retaining independent review evidence."""
        previous = previous_review.submission
        if (previous.target != plan.ref() or previous.target_kind != "PLAN" or previous.author != DAEDALUS
                or previous.reviewer != MOMUS or previous.reviewer_role != MOMUS
                or previous.input_refs != [req.ref(), ds.ref()]
                or any(f.target != plan.ref() for f in previous.findings)):
            raise ValueError("program revalidation requires the exact independently reviewed baseline")
        recorded = CommonReviewer().validate_and_record(previous, current_target=plan.ref(),
                                                         expected_author=DAEDALUS,
                                                         current_inputs=[req.ref(), ds.ref()])
        if recorded.review != previous_review.review or recorded.blocking_finding_ids != previous_review.blocking_finding_ids:
            raise ValueError("program revalidation requires a consistent review record")
        if (not set(PLAN_REVIEW_CHECKS) <= set(previous.required_checks)
                or "momus:semantic-review" not in previous.evidence
                or not any(item.startswith("ER-") for item in previous.evidence)):
            raise ValueError("program revalidation requires recorded independent semantic review evidence")
        semantic = [f.model_copy(deep=True) for f in previous.findings if f.raised_by == MOMUS]
        citation_fields = ("goal", "type", "sources", "inputs", "outputs", "depends_on", "checks",
                           "write_scope", "executor", "reviewer", "resources")
        valid_citations = set()
        for task in plan.tasks:
            for field in citation_fields:
                value = getattr(task, field)
                for quote in [value] if isinstance(value, str) else value:
                    valid_citations.add(f"{task.task_id} · {field}：{quote}")
        if any(f.status == "closed" and (f.closed_by != MOMUS or not f.closure_reason
                                         or not set(f.closure_reason.splitlines()) <= valid_citations)
               for f in semantic):
            raise ValueError("closed semantic findings require independent closure evidence")
        report, findings = self._program_findings(plan, req, ds)
        common_findings = []
        for index, finding in enumerate(findings):
            current = self._record_finding(plan, finding, index, "program")
            # New rules can add findings on the same version; use a separate
            # namespace so their IDs cannot collide with the retained semantic list.
            current.finding_id = f"F-P-{plan.object_id[:105]}-V{plan.version}-{index}"
            common_findings.append(current)
        common_findings.extend(semantic)
        findings.extend(SemanticFinding(code=f.category, severity=f.severity, message=f.message)
                        for f in semantic if f.status == "open")
        blocked = any(f.severity == "blocker" and f.status == "open" for f in common_findings)
        suffix = "-REVALIDATED-" + fingerprint({
            "previous": previous.review_id,
            "program": report.model_dump(mode="json"),
            "program_findings": [f.model_dump(mode="json") for f in common_findings if f.raised_by == "program"],
        })[:8]
        submission = previous.model_copy(update={
            "review_id": previous.review_id[:160 - len(suffix)] + suffix,
            "verdict": "blocked" if blocked else "passed", "findings": common_findings,
            "required_checks": self._required_checks(previous.required_checks),
            "passed_checks": self._passed_checks(common_findings, self._required_checks(previous.required_checks)),
            "evidence": list(dict.fromkeys([*previous.evidence, previous.review_id,
                                            "program:revalidated-identical-plan", *self._required_checks([])])),
        }, deep=True)
        record = self.common_reviewer.validate_and_record(submission, current_target=plan.ref(),
                                                           expected_author=DAEDALUS,
                                                           current_inputs=[req.ref(), ds.ref()])
        return PlanReview(review=record.review, validation=report, findings=findings,
                          revision_recommended=blocked, record=record)

    def review(self, plan: Plan, req: Requirement, ds: Design, *,
               previous_review: ReviewRecord | None = None) -> PlanReview:
        report, findings = self._program_findings(plan, req, ds)

        common_findings = [
            self._record_finding(plan, finding, index, "program")
            for index, finding in enumerate(findings)
        ]
        execution_evidence = []
        mode = self.mode
        checks = ["program:validate_plan_semantics", "momus:heuristic-review"]
        if previous_review is not None:
            previous = previous_review.submission
            if (previous.target_kind != "PLAN" or previous.author != DAEDALUS or previous.reviewer != self.reviewer_id
                    or previous.input_refs != [req.ref(), ds.ref()] or previous.target.object_id != plan.object_id
                    or previous.target.version + 1 != plan.version):
                raise ValueError("plan revision must follow the reviewed baseline")
            carried = []
            for finding in previous.findings:
                if finding.raised_by != self.reviewer_id:
                    continue
                update = {"target": plan.ref()}
                if finding.severity == "blocker":
                    update.update(status="open", closed_by=None, closure_reason=None)
                carried.append(finding.model_copy(update=update))
            common_findings.extend(carried)
            blockers = [finding for finding in carried if finding.status == "open"]
            execution_evidence.append(previous.review_id)
            if blockers and self.runner is not None and self.runner.provider.mode == "real_model":
                execution_evidence.append(self._recheck_agent(plan, req, ds, previous_review, blockers))
            findings.extend(SemanticFinding(code=f.category, severity=f.severity, message=f.message)
                            for f in carried if f.status == "open")
            mode, checks = previous.mode, previous.required_checks
        elif self.runner is not None:
            agent_findings, role_result = self._run_agent(plan, req, ds)
            common_findings.extend(self._record_finding(plan, finding, index, self.reviewer_id)
                                   for index, finding in enumerate(agent_findings, len(findings)))
            findings.extend(agent_findings)
            execution_evidence.append(role_result.execution_record_id)
            mode = role_result.mode
            checks = list(PLAN_REVIEW_CHECKS)

        checks = self._required_checks(checks)
        blocked = any(item.severity == "blocker" and item.status == "open" for item in common_findings)
        verdict = "blocked" if blocked else "passed"
        submission = ReviewSubmission(
            review_id=f"REV-{plan.object_id[:100]}-V{plan.version}"[:160],
            target=plan.ref(),
            author=DAEDALUS,
            reviewer=self.reviewer_id,
            reviewer_role="Momus",
            target_kind="PLAN",
            verdict=verdict,
            findings=common_findings,
            evidence=[
                f"validation:{report.plan_ref.object_id}:v{report.plan_ref.version}",
                *checks,
                *execution_evidence,
            ],
            input_refs=[req.ref(), ds.ref()],
            required_checks=checks,
            passed_checks=self._passed_checks(common_findings, checks),
            mode=mode,
        )
        record = self.common_reviewer.validate_and_record(
            submission,
            current_target=plan.ref(),
            expected_author=DAEDALUS,
            current_inputs=[req.ref(), ds.ref()],
        )
        return PlanReview(
            review=record.review,
            validation=report,
            findings=findings,
            revision_recommended=blocked,
            record=record,
        )

    @staticmethod
    def _record_finding(plan, finding, index, origin):
        task = f" ({finding.task_id})" if finding.task_id else ""
        return ReviewFinding(
            finding_id=f"F-{plan.object_id[:115]}-V{plan.version}-{index}",
            target=plan.ref(), category=finding.code, severity=finding.severity,
            message=f"[{finding.severity}] {finding.code}{task}: {finding.message}", raised_by=origin,
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
        closed = Review(
            review_id=f"{review.review_id}-CLOSE-{closer}"[:160],
            target=plan.ref(),
            author=review.author,
            reviewer=closer,
            verdict=verdict,
            findings=list(review.findings),
            evidence=evidence or list(review.evidence),
            mode=review.mode,
        )
        try:
            record = self.common_reviewer.get(review.review_id)
        except KeyError:
            record = None
        if record is not None:
            for finding in record.submission.findings:
                if finding.status == "open":
                    self.common_reviewer.close_finding(
                        review.review_id,
                        finding.finding_id,
                        actor=closer,
                        reason="closed via close_blockers",
                        authorized_non_authors={self.reviewer_id, *self.authorized_closers},
                    )
        return closed


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

    def run(self, req: Requirement, ds: Design, max_revisions: int | None = None, *,
            initial_plan: Plan | None = None, previous_review: ReviewRecord | None = None,
            on_review: Callable[[Plan, ReviewRecord], None] | None = None) -> PlanningOutcome:
        limit = self.max_revisions if max_revisions is None else self._revision_limit(max_revisions)
        plan = initial_plan or self.planner.plan(req, ds)
        reviews: list[Review] = []
        revisions_used = 0
        while True:
            result = self.reviewer.review(plan, req, ds, previous_review=previous_review)
            if result.review.target != plan.ref():
                raise ValueError("review target must bind the reviewed plan version")
            reviews.append(result.review)
            if on_review is not None:
                on_review(plan, result.record)
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
            blockers = [item.message for item in result.record.submission.findings
                        if item.finding_id in result.record.blocking_finding_ids]
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
                    findings=blockers,
                )
            active_findings = [item.message for item in result.record.submission.findings
                               if item.status == "open" and item.severity in {"blocker", "important"}]
            revised = self.planner.revise_plan(plan, active_findings, req, ds)
            if revised.version != plan.version + 1 or revised.ref() == plan.ref():
                raise ValueError("revision must bump the plan version and its content hash")
            plan = revised
            previous_review = result.record
            revisions_used += 1

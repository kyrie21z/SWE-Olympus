"""P4 design orchestration; P3 still owns the human wait/resume state machine."""

from dataclasses import dataclass
from copy import deepcopy
from datetime import UTC, datetime
import threading

from olympus.contracts.models import Confirmation, Design, Requirement, fingerprint
from olympus.requirements_bridge import RequirementBridge
from olympus.reviews.common import ReviewRecord

from .designer import AthenaDesigner


@dataclass(frozen=True)
class DesignOutcome:
    status: str
    design: Design
    reports: tuple[ReviewRecord, ...]
    revisions_used: int


class DesignWorkflow:
    def __init__(self, bridge: RequirementBridge, designer: AthenaDesigner, reviewer):
        self.bridge, self.designer, self.reviewer = bridge, designer, reviewer
        self._designs: dict[str, Design] = {}
        self._reports: dict[str, str] = {}
        self._approvals: dict[str, Confirmation] = {}
        self._outcomes: dict[str, DesignOutcome] = {}
        self._revision_counts: dict[str, int] = {}
        self._lock = threading.RLock()

    def generate(self, req: Requirement, *, run_id="RUN-design", max_revisions=1) -> DesignOutcome:
        with self._lock:
            return deepcopy(self._generate(req, run_id=run_id, max_revisions=max_revisions))

    def _generate(self, req: Requirement, *, run_id, max_revisions) -> DesignOutcome:
        if max_revisions not in (0, 1):
            raise ValueError("at most one content revision")
        self.bridge.require_current(req)
        cycle = fingerprint(req.ref().model_dump(mode="json"))
        if cycle in self._outcomes:
            return self._outcomes[cycle]
        previous = self._designs.get(f"DS-{req.object_id[:120]}")
        if previous and previous.requirement != req.ref():
            # New baseline starts a new design version, but must not reuse old content.
            previous_for_version = previous
            ds = self.designer.generate(req, run_id=run_id)
            ds = Design(**(ds.model_dump(mode="json") | {"version": previous_for_version.version + 1}))
        else:
            ds = self.designer.generate(req, run_id=run_id, previous=previous)
        reports = []
        for revision in range(max_revisions + 1):
            self._designs[ds.object_id] = ds.model_copy(deep=True)
            self._approvals.pop(ds.object_id, None)
            report = self.reviewer.review(req, ds, run_id=run_id, previous_review=reports[-1] if reports else None)
            report = self.reviewer.recheck(req, ds, report, run_id=run_id)
            reports.append(report)
            self._reports[ds.object_id] = report.review.review_id
            if report.review.verdict == "passed":
                outcome = DesignOutcome("awaiting_approval", ds, tuple(reports), revision)
                self._outcomes[cycle] = outcome
                self._revision_counts[cycle] = revision
                return outcome
            if revision < max_revisions:
                blockers = [finding for finding in report.submission.findings
                            if finding.finding_id in report.blocking_finding_ids]
                ds = self.designer.generate(req, run_id=run_id, previous=ds,
                                             findings=blockers)
        outcome = DesignOutcome("blocked_awaiting_human", ds, tuple(reports), max_revisions)
        self._outcomes[cycle] = outcome
        self._revision_counts[cycle] = max_revisions
        return outcome

    def revise(self, req: Requirement, *, findings: list[str], run_id="RUN-design") -> DesignOutcome:
        """One explicit content revision per requirement baseline, including auto-repair."""
        with self._lock:
            return deepcopy(self._revise(req, findings=findings, run_id=run_id))

    def _revise(self, req: Requirement, *, findings, run_id) -> DesignOutcome:
        self.bridge.require_current(req)
        cycle = fingerprint(req.ref().model_dump(mode="json"))
        if cycle not in self._outcomes or not findings:
            raise ValueError("revision requires an existing design and recorded reasons")
        if self._revision_counts.get(cycle, 0) >= 1:
            raise ValueError("content revision limit reached; needs human/upstream resolution")
        self._revision_counts[cycle] = 1
        old = self._outcomes[cycle]
        ds = self.designer.generate(req, run_id=run_id, previous=old.design, findings=findings)
        self._designs[ds.object_id] = ds.model_copy(deep=True)
        self._approvals.pop(ds.object_id, None)
        previous_review = old.reports[-1] if old.status == "blocked_awaiting_human" else None
        report = self.reviewer.review(req, ds, run_id=run_id, previous_review=previous_review)
        report = self.reviewer.recheck(req, ds, report, run_id=run_id)
        self._reports[ds.object_id] = report.review.review_id
        outcome = DesignOutcome("awaiting_approval" if report.review.verdict == "passed" else
                                "blocked_awaiting_human", ds, (*old.reports, report), 1)
        self._outcomes[cycle] = outcome
        return outcome

    def _validate_current(self, req: Requirement, ds: Design) -> ReviewRecord:
        ds = Design.model_validate(ds.model_dump(mode="json"))
        self.bridge.require_current(req)
        current = self._designs.get(ds.object_id)
        if current is None or current.ref() != ds.ref() or ds.requirement != req.ref():
            raise ValueError("design or upstream requirement is stale")
        report = self.reviewer.common.get(self._reports[ds.object_id])
        if (report.submission.input_refs != [req.ref()] or
                not self.reviewer.common.verify_for_acceptance(report.review.review_id,
                                                               current_target=ds.ref()) or
                self.reviewer.program_findings(req, ds)):
            raise ValueError("current independent design report is not passed")
        return report

    def bind_approval(self, bus, req: Requirement, ds: Design):
        """Add business guards to P3's command registry, without another approval state machine."""
        with self._lock:
            report = self._validate_current(req, ds)
        bus.bind_target(ds.ref())

        def approve(command, ctx, scheduler):
            del scheduler
            with self._lock, self.bridge._lock:
                self._validate_current(req, ds)
                if command.target_id != ds.object_id or command.expected_version != ds.version:
                    raise ValueError("approval targets another design")
                if ctx.run.status != "draft":
                    raise ValueError("design approval requires a draft run")
                admission = self.bridge.require_current(req)
                confirmation = Confirmation(
                    confirmation_id=command.operation_id, actor=command.actor,
                    origin="synthetic_fixture" if admission.synthetic else "human",
                    target=ds.ref(), dependencies=[req.ref()], confirmed_at=datetime.now(UTC),
                )
                ctx.commit(reason=f"design approved: {ds.object_id} v{ds.version}",
                           actor=command.actor, actor_type="human", mutate=lambda snapshot: None)
                self._approvals[ds.object_id] = confirmation
            return "accepted", "ok", "design approved; execution has not started"

        bus.register("approve_design", approve, {"YOU"},
                     "current REQ/DS and independent report", "record design approval")
        return report

    def request_approval(self, gate, req: Requirement, ds: Design):
        """Register the design-specific guard, then use the P3 gate unchanged."""
        report = self.bind_approval(gate.bus, req, ds)
        return gate.start(
            kind="design_approval", target_id=ds.object_id, expected_version=ds.version,
            req_ref=req.ref().model_dump(mode="json"), ds_ref=ds.ref().model_dump(mode="json"),
            required_reports=[report.review.review_id], available_reports=[report.review.review_id],
            payload=self.project(req, ds),
        )

    def require_approved(self, req: Requirement, ds: Design) -> Confirmation:
        with self._lock, self.bridge._lock:
            self._validate_current(req, ds)
            confirmation = self._approvals.get(ds.object_id)
            if confirmation is None or confirmation.target != ds.ref():
                raise ValueError("design has not been approved at this version")
            return confirmation.model_copy(deep=True)

    def plan(self, req: Requirement, ds: Design, planning_workflow):
        self.require_approved(req, ds)
        result = planning_workflow.run(req, ds)
        self.require_approved(req, ds)
        return result

    def project(self, req: Requirement, ds: Design) -> dict:
        with self._lock:
            return self._project(req, ds)

    def _project(self, req: Requirement, ds: Design) -> dict:
        ds = Design.model_validate(ds.model_dump(mode="json"))
        admission = self.bridge.require_current(req)
        current = self._designs.get(ds.object_id)
        is_current = current is not None and current.ref() == ds.ref() and ds.requirement == req.ref()
        report = next((r for r in self.reviewer.common.list_reviews()
                       if r.review.target == ds.ref() and r.submission.input_refs == [req.ref()]), None)
        confirmation = self._approvals.get(ds.object_id)
        return {
            "requirement": req.ref().model_dump(mode="json"),
            "design": ds.model_dump(mode="json"),
            "synthetic_fixture": admission.synthetic,
            "is_current": is_current,
            "review_id": report.review.review_id if report else None,
            "confirmation": (confirmation.model_dump(mode="json") if is_current and confirmation and
                             confirmation.target == ds.ref() else None),
            "traceability": [{"requirement": source, "design_element": e["id"],
                              "acceptance_method": e["acceptance_method"]}
                             for e in ds.content["elements"] for source in e["sources"]],
        }

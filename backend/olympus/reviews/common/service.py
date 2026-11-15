"""Independent review service with role isolation, fingerprint staleness and closure authority."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, ValidationError

from olympus.contracts.models import Contract, Id, Mode, Ref, Review

TargetKind = Literal["DS", "PLAN", "TASK_ARTIFACT"]
ReviewerRole = Literal["Momus", "Argus"]
Verdict = Literal["passed", "blocked", "needs_revision"]
Severity = Literal["blocker", "important", "nit"]
FindingStatus = Literal["open", "closed"]

IMPLEMENTATION_ROLES = frozenset({"Hephaestus", "Daedalus", "Athena"})
REVIEWER_FOR_TARGET: dict[str, ReviewerRole] = {
    "DS": "Momus",
    "PLAN": "Momus",
    "TASK_ARTIFACT": "Argus",
}


class ReviewIsolationError(ValueError):
    """Reviewer identity or role violates independence."""


class StaleReviewError(ValueError):
    """Review was produced against an older target, inputs or artifact."""


class ReviewPermissionError(ValueError):
    """Actor may not close findings for this review."""


class ReviewValidationError(ValueError):
    """Submission cannot be accepted as a passed review."""


class ReviewFinding(Contract):
    finding_id: Id
    target: Ref
    category: Id
    severity: Severity
    message: Id
    raised_by: Id
    status: FindingStatus = "open"
    closed_by: str | None = None
    closure_reason: str | None = None


class ReviewSubmission(Contract):
    review_id: Id
    target: Ref
    author: Id
    reviewer: Id
    reviewer_role: ReviewerRole
    target_kind: TargetKind
    verdict: Verdict
    findings: list[ReviewFinding] = Field(default_factory=list)
    evidence: list[Id] = Field(default_factory=list)
    input_refs: list[Ref] = Field(default_factory=list)
    artifact_ref: Ref | None = None
    required_checks: list[Id] = Field(default_factory=list)
    passed_checks: list[Id] = Field(default_factory=list)
    mode: Mode = "mock"


class ReviewRecord(Contract):
    review: Review
    submission: ReviewSubmission
    target_kind: TargetKind
    blocking_finding_ids: list[Id]


class CommonReviewer:
    """One engine, two reviewer personas. Findings belong to reviewers, not authors."""

    def __init__(self) -> None:
        self._records: dict[str, ReviewRecord] = {}

    # ------------------------------------------------------------------ queries
    def get(self, review_id: str) -> ReviewRecord:
        try:
            return self._records[review_id]
        except KeyError:
            raise KeyError(review_id) from None

    def list_reviews(self) -> list[ReviewRecord]:
        return list(self._records.values())

    def __len__(self) -> int:
        return len(self._records)

    # ------------------------------------------------------------------ protocol
    def validate_and_record(
        self,
        submission: ReviewSubmission,
        *,
        current_target: Ref,
        expected_author: str | None = None,
        current_inputs: list[Ref] | None = None,
        current_artifact_hash: str | None = None,
    ) -> ReviewRecord:
        self._check_isolation(submission)
        if submission.target != current_target:
            raise StaleReviewError("review target version or content_hash does not match current target")
        if expected_author is not None and submission.author != expected_author:
            raise ReviewIsolationError("author mismatch")
        if current_inputs is not None and submission.input_refs != current_inputs:
            raise StaleReviewError("review input_refs do not match current approved inputs")
        if current_artifact_hash is not None and (
            not submission.artifact_ref or submission.artifact_ref.content_hash != current_artifact_hash
        ):
            raise StaleReviewError("artifact hash mismatch")
        if submission.verdict == "passed":
            missing = [check for check in submission.required_checks if check not in submission.passed_checks]
            if missing:
                raise ReviewValidationError(
                    "required checks not satisfied: " + ", ".join(sorted(missing))
                )
            open_blockers = [
                finding.finding_id
                for finding in submission.findings
                if finding.severity == "blocker" and finding.status == "open"
            ]
            if open_blockers:
                raise ReviewValidationError(
                    "open blocker findings prevent a passed verdict: " + ", ".join(sorted(open_blockers))
                )
        try:
            canonical = Review(
                review_id=submission.review_id,
                target=submission.target,
                author=submission.author,
                reviewer=submission.reviewer,
                verdict=submission.verdict,
                findings=[finding.message for finding in submission.findings],
                evidence=submission.evidence,
                mode=submission.mode,
            )
        except ValidationError as exc:
            raise ReviewValidationError(f"review cannot be recorded: {exc}") from exc
        record = ReviewRecord(
            review=canonical,
            submission=submission.model_copy(deep=True),
            target_kind=submission.target_kind,
            blocking_finding_ids=self._blocking_ids(submission),
        )
        self._records[submission.review_id] = record
        return record

    def close_finding(
        self,
        review_id: str,
        finding_id: str,
        *,
        actor: str,
        reason: str,
        authorized_non_authors: set[str] | None = None,
    ) -> ReviewFinding:
        record = self.get(review_id)
        submission = record.submission
        finding = next((item for item in submission.findings if item.finding_id == finding_id), None)
        if finding is None:
            raise KeyError(finding_id)
        authorized = authorized_non_authors or set()
        if actor == submission.author:
            raise ReviewPermissionError("author or unauthorized role cannot close review findings")
        if actor != finding.raised_by and actor not in authorized:
            raise ReviewPermissionError("author or unauthorized role cannot close review findings")
        finding.status = "closed"
        finding.closed_by = actor
        finding.closure_reason = reason
        record.blocking_finding_ids = self._blocking_ids(submission)
        return finding.model_copy(deep=True)

    def verify_for_acceptance(self, review_id: str, *, current_target: Ref) -> bool:
        record = self._records.get(review_id)
        if record is None:
            return False
        if record.review.target != current_target:
            return False
        if record.review.verdict != "passed":
            return False
        return all(
            finding.status == "closed"
            for finding in record.submission.findings
            if finding.severity == "blocker"
        )

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _check_isolation(submission: ReviewSubmission) -> None:
        if submission.author == submission.reviewer:
            raise ReviewIsolationError("author cannot review their own work")
        if submission.reviewer in IMPLEMENTATION_ROLES:
            raise ReviewIsolationError("implementation role cannot act as independent reviewer")
        expected = REVIEWER_FOR_TARGET[submission.target_kind]
        if submission.reviewer_role != expected:
            raise ReviewIsolationError(
                f"{submission.target_kind} must be reviewed by {expected}, not {submission.reviewer_role}"
            )

    @staticmethod
    def _blocking_ids(submission: ReviewSubmission) -> list[Id]:
        return [
            finding.finding_id
            for finding in submission.findings
            if finding.severity == "blocker" and finding.status == "open"
        ]


IndependentReviewService = CommonReviewer

"""Independent review engine: isolation, staleness, required checks and closure authority."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from olympus.contracts.models import Ref
from olympus.reviews.common import (
    CommonReviewer,
    IndependentReviewService,
    ReviewFinding,
    ReviewIsolationError,
    ReviewPermissionError,
    ReviewSubmission,
    ReviewValidationError,
    StaleReviewError,
)

ROOT = Path(__file__).resolve().parents[2]
DS_CURRENT = Ref(object_id="DS-001", version=2, content_hash="a" * 64)
DS_OLD = Ref(object_id="DS-001", version=1, content_hash="b" * 64)
REQ_CURRENT = Ref(object_id="REQ-001", version=2, content_hash="c" * 64)
REQ_OLD = Ref(object_id="REQ-001", version=1, content_hash="d" * 64)
ART_CURRENT = Ref(object_id="ART-001", version=2, content_hash="e" * 64)


def finding(
    finding_id: str = "F-1",
    severity: str = "blocker",
    status: str = "open",
    raised_by: str = "Momus",
    target: Ref = DS_CURRENT,
) -> ReviewFinding:
    return ReviewFinding(
        finding_id=finding_id,
        target=target,
        category="design",
        severity=severity,
        message=f"finding {finding_id}",
        raised_by=raised_by,
        status=status,
    )


def submission(**overrides) -> ReviewSubmission:
    payload = {
        "review_id": "REV-001",
        "target": DS_CURRENT,
        "author": "Daedalus",
        "reviewer": "Momus",
        "reviewer_role": "Momus",
        "target_kind": "DS",
        "verdict": "passed",
        "findings": [],
        "evidence": ["EV-001"],
        "input_refs": [REQ_CURRENT],
        "artifact_ref": None,
        "required_checks": ["DS-COMPLETE"],
        "passed_checks": ["DS-COMPLETE"],
        "mode": "mock",
    }
    payload.update(overrides)
    return ReviewSubmission(**payload)


def record(submission_value: ReviewSubmission, reviewer: CommonReviewer | None = None):
    service = reviewer or CommonReviewer()
    return service, service.validate_and_record(submission_value, current_target=submission_value.target)


# --------------------------------------------------------------------------- isolation
def test_author_cannot_review_their_own_work():
    with pytest.raises(ReviewIsolationError):
        CommonReviewer().validate_and_record(
            submission(reviewer="Daedalus", reviewer_role="Momus"), current_target=DS_CURRENT
        )


@pytest.mark.parametrize("role", ["Hephaestus", "Daedalus", "Athena"])
def test_implementation_roles_cannot_be_independent_reviewers(role):
    with pytest.raises(ReviewIsolationError):
        CommonReviewer().validate_and_record(
            submission(author="SomeoneElse", reviewer=role, reviewer_role="Momus"),
            current_target=DS_CURRENT,
        )


def test_reviewer_role_must_match_target_kind():
    with pytest.raises(ReviewIsolationError):
        CommonReviewer().validate_and_record(
            submission(reviewer_role="Argus"), current_target=DS_CURRENT
        )
    with pytest.raises(ReviewIsolationError):
        CommonReviewer().validate_and_record(
            submission(
                target=ART_CURRENT,
                target_kind="TASK_ARTIFACT",
                reviewer="Momus",
                reviewer_role="Momus",
                artifact_ref=ART_CURRENT,
            ),
            current_target=ART_CURRENT,
            current_artifact_hash=ART_CURRENT.content_hash,
        )


def test_expected_author_mismatch_is_isolation_error():
    with pytest.raises(ReviewIsolationError, match="author mismatch"):
        CommonReviewer().validate_and_record(
            submission(), current_target=DS_CURRENT, expected_author="Hephaestus"
        )


# --------------------------------------------------------------------------- staleness
def test_stale_target_version_or_hash_is_rejected():
    with pytest.raises(StaleReviewError, match="does not match current target"):
        CommonReviewer().validate_and_record(submission(target=DS_OLD), current_target=DS_CURRENT)


def test_stale_input_refs_are_rejected():
    with pytest.raises(StaleReviewError, match="input_refs"):
        CommonReviewer().validate_and_record(
            submission(input_refs=[REQ_OLD]), current_target=DS_CURRENT, current_inputs=[REQ_CURRENT]
        )


def test_missing_or_stale_artifact_hash_is_rejected():
    artifact_submission = submission(
        target=ART_CURRENT,
        target_kind="TASK_ARTIFACT",
        reviewer="Argus",
        reviewer_role="Argus",
        artifact_ref=ART_CURRENT,
    )
    with pytest.raises(StaleReviewError, match="artifact hash mismatch"):
        CommonReviewer().validate_and_record(
            artifact_submission,
            current_target=ART_CURRENT,
            current_artifact_hash="f" * 64,
        )
    no_artifact = submission(
        target=ART_CURRENT,
        target_kind="TASK_ARTIFACT",
        reviewer="Argus",
        reviewer_role="Argus",
        artifact_ref=None,
    )
    with pytest.raises(StaleReviewError, match="artifact hash mismatch"):
        CommonReviewer().validate_and_record(
            no_artifact,
            current_target=ART_CURRENT,
            current_artifact_hash=ART_CURRENT.content_hash,
        )


def test_stale_fixture_references_old_design_version():
    payload = json.loads((ROOT / "fixtures/agents/stale_review.json").read_text(encoding="utf-8"))
    stale = ReviewSubmission.model_validate(payload)
    with pytest.raises(StaleReviewError):
        CommonReviewer().validate_and_record(stale, current_target=DS_CURRENT)


# --------------------------------------------------------------------------- validation
def test_passed_requires_all_required_checks():
    with pytest.raises(ReviewValidationError, match="required checks"):
        CommonReviewer().validate_and_record(
            submission(
                required_checks=["DS-COMPLETE", "TRACEABILITY"],
                passed_checks=["DS-COMPLETE"],
            ),
            current_target=DS_CURRENT,
        )


def test_passed_rejects_open_blocker_findings():
    with pytest.raises(ReviewValidationError, match="blocker"):
        CommonReviewer().validate_and_record(
            submission(findings=[finding()]), current_target=DS_CURRENT
        )


def test_blocked_verdict_may_keep_open_findings():
    service, stored = record(submission(verdict="blocked", findings=[finding()]))
    assert stored.review.verdict == "blocked"
    assert stored.blocking_finding_ids == ["F-1"]
    assert service.verify_for_acceptance("REV-001", current_target=DS_CURRENT) is False


def test_evidence_is_required_to_build_canonical_review():
    with pytest.raises(ReviewValidationError):
        CommonReviewer().validate_and_record(
            submission(verdict="blocked", findings=[], evidence=[]), current_target=DS_CURRENT
        )


def test_successful_submission_records_canonical_review():
    service, stored = record(submission(findings=[finding("F-NIT", severity="nit")]))
    assert stored.review.review_id == "REV-001"
    assert stored.review.author == "Daedalus"
    assert stored.review.reviewer == "Momus"
    assert stored.review.verdict == "passed"
    assert stored.review.findings == ["finding F-NIT"]
    assert stored.target_kind == "DS"
    assert stored.blocking_finding_ids == []
    assert service.verify_for_acceptance("REV-001", current_target=DS_CURRENT) is True
    assert IndependentReviewService is CommonReviewer


# --------------------------------------------------------------------------- closure
def test_author_cannot_close_findings():
    service, _ = record(submission(verdict="blocked", findings=[finding(raised_by="Momus")]))
    with pytest.raises(ReviewPermissionError, match="author or unauthorized role"):
        service.close_finding("REV-001", "F-1", actor="Daedalus", reason="looks fine")


def test_unauthorized_actor_cannot_close_findings():
    service, _ = record(submission(verdict="blocked", findings=[finding(raised_by="Momus")]))
    with pytest.raises(ReviewPermissionError):
        service.close_finding("REV-001", "F-1", actor="Argus", reason="trust me")


def test_reviewer_closes_blocker_and_acceptance_becomes_verifiable():
    service, stored = record(submission(verdict="blocked", findings=[finding(raised_by="Momus")]))
    assert stored.blocking_finding_ids == ["F-1"]
    closed = service.close_finding("REV-001", "F-1", actor="Momus", reason="fixed in v2")
    assert closed.status == "closed"
    assert closed.closed_by == "Momus"
    assert closed.closure_reason == "fixed in v2"
    assert service.get("REV-001").blocking_finding_ids == []
    assert service.verify_for_acceptance("REV-001", current_target=DS_CURRENT) is False
    # The verdict is still blocked; closure alone cannot rewrite it.
    assert service.get("REV-001").review.verdict == "blocked"


def test_authorized_non_author_can_close_but_author_still_cannot():
    service, _ = record(submission(verdict="blocked", findings=[finding(raised_by="Momus")]))
    closed = service.close_finding(
        "REV-001",
        "F-1",
        actor="Hephaestus",
        reason="human reviewed",
        authorized_non_authors={"Hephaestus"},
    )
    assert closed.status == "closed"
    with pytest.raises(ReviewPermissionError):
        service.close_finding(
            "REV-001", "F-1", actor="Daedalus", reason="self-close", authorized_non_authors={"Daedalus"}
        )


def test_closed_blocker_on_passed_review_can_be_verified():
    service, _ = record(submission(findings=[]))
    assert service.verify_for_acceptance("REV-001", current_target=DS_CURRENT) is True
    assert service.verify_for_acceptance("REV-001", current_target=DS_OLD) is False
    assert service.verify_for_acceptance("REV-missing", current_target=DS_CURRENT) is False


def test_missing_review_or_finding_raises_key_error():
    service = CommonReviewer()
    with pytest.raises(KeyError):
        service.close_finding("REV-missing", "F-1", actor="Momus", reason="x")
    service, _ = record(submission(verdict="blocked", findings=[]))
    with pytest.raises(KeyError):
        service.close_finding("REV-001", "F-missing", actor="Momus", reason="x")

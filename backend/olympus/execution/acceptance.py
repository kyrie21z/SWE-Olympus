"""The trusted bridge from real evidence and independent review to Zeus state transitions."""

import threading

from olympus.contracts.models import Ref, fingerprint
from olympus.agents.runtime.models import (
    OutputSchemaError, ProviderNotConfiguredError, RunnerPermissionError, UnknownOutcomeError,
)


class ArtifactAcceptanceService:
    def __init__(self, executor, reviewer):
        self.executor = executor
        self.reviewer = reviewer
        self.scheduler = executor.scheduler
        self._collect_lock = threading.RLock()

    def cancel(self, task_id: str):
        """P5 entry point: verify actual termination before releasing scheduler resources."""
        attempt_id = self.scheduler.attempt_for(task_id)
        if attempt_id is None:
            raise ValueError("task has not been dispatched")
        receipt = self.executor.cancel(attempt_id)
        with self.scheduler._lock:
            state = self.scheduler.ctx.state_of(task_id)
            if state in {"accepted", "cancelled"}:
                return receipt
            if receipt.state in {"succeeded", "failed", "cancelled"}:
                self.scheduler.cancel(task_id, reason="P4 verified local execution is terminal")
            elif state == "running":
                self.scheduler.ctx.transition(task_id, "blocked", reason="cancellation not confirmed",
                                              actor="Zeus", actor_type="program")
                self.scheduler.release(task_id, keep_resources=True)
                self.scheduler.register_incident(task_id, "outcome_unknown", "cancellation needs reconciliation")
        return receipt

    def collect(self, task_id: str, attempt_id: str, *, review_id: str | None = None) -> tuple[str, str]:
        with self._collect_lock:
            return self._collect(task_id, attempt_id, review_id=review_id)

    def _collect(self, task_id: str, attempt_id: str, *, review_id=None) -> tuple[str, str]:
        scheduler = self.scheduler
        receipt = self.executor.query_receipt(attempt_id)
        with scheduler._lock:
            runtime = scheduler.ctx.runtime(task_id)
            if (receipt.run_id != scheduler.ctx.run.run_id or receipt.task_id != task_id or
                    runtime.generation != receipt.generation or
                    scheduler.attempt_for(task_id) != attempt_id or
                    runtime.state not in {"running", "verifying", "accepted"}):
                return "rejected", "stale, cancelled or superseded execution"
            task = scheduler.tasks[task_id]
            if receipt.task_hash != task.content_hash:
                return "rejected", "task changed after execution"
            if runtime.state == "accepted":
                if receipt.review is None or (review_id and review_id != receipt.review.review.review_id):
                    return "rejected", "accepted artifact has no matching persisted independent report"
                try:
                    self.executor.verify_integrity(attempt_id)
                    self._validate_receipt(task, receipt, history=True)
                    record = self._restore_report(task, receipt, receipt.review)
                except (ValueError, OSError, KeyError) as exc:
                    return "rejected", str(exc)
                return ("accepted", record.review.review_id) if self._report_passes(task, receipt, record) else (
                    "rejected", "persisted independent report is not valid")
            if receipt.state == "running":
                return "pending", "execution is still in flight"
            if receipt.state == "outcome_unknown":
                scheduler.ctx.transition(task_id, "blocked", reason="execution outcome requires reconciliation",
                                         actor="Zeus", actor_type="program")
                scheduler.release(task_id, keep_resources=True)
                scheduler.register_incident(task_id, "outcome_unknown", "query provider before retry")
                return "pending", "outcome unknown; resources retained"
            if receipt.state == "cancelled":
                scheduler.cancel(task_id, reason="local process termination confirmed")
                return "rejected", "execution cancelled"
            if receipt.state == "failed":
                scheduler.ctx.transition(task_id, "failed", reason=receipt.error or "execution failed",
                                         actor="Zeus", verification="failed")
                scheduler.release(task_id)
                return "rejected", receipt.error or "program checks failed"
            try:
                actual = self.executor.verify_integrity(attempt_id)
                self._validate_receipt(task, receipt)
            except (ValueError, OSError) as exc:
                return "rejected", str(exc)
            if runtime.state == "running":
                status, reason = scheduler.accept_submission(
                    task_id, receipt.generation, [a.artifact_id for a in receipt.artifacts],
                    checks_passed=receipt.checks_passed)
                if status != "accepted":
                    return "rejected", reason

        # A role invocation may take time: cancellation can win while Argus is working.
        if review_id is None:
            if receipt.review is not None:
                try:
                    record = self._restore_report(task, receipt, receipt.review)
                except ValueError as exc:
                    return "rejected", str(exc)
            else:
                previous = self._review_requests(receipt)
                if previous:
                    return "pending", "existing independent-review request needs reconciliation or explicit retry"
                try:
                    with self.executor.model_budget(self.reviewer.runner) as budget:
                        record = self.reviewer.review(task, receipt, actual_files=actual, budget=budget)
                except (OutputSchemaError, ProviderNotConfiguredError, RunnerPermissionError, UnknownOutcomeError) as exc:
                    scheduler.register_incident(task_id, "artifact_missing", f"independent review unavailable: {exc}")
                    return "pending", "independent review unavailable; artifact is still unverified"
                self.executor.record_review(attempt_id, record)
        else:
            try:
                record = self.reviewer.common.get(review_id)
            except KeyError:
                return "rejected", "independent report does not exist"

        with scheduler._lock:
            runtime = scheduler.ctx.runtime(task_id)
            if (runtime.state != "verifying" or runtime.generation != receipt.generation or
                    scheduler.attempt_for(task_id) != attempt_id):
                return "rejected", "execution changed while the artifact was reviewed"
            try:
                self.executor.verify_integrity(attempt_id)
                self._validate_receipt(scheduler.tasks[task_id], receipt)
            except (ValueError, OSError) as exc:
                return "rejected", str(exc)
            target = self._target(receipt)
            if not self._report_passes(task, receipt, record):
                if record.review.target == target and record.review.verdict != "passed":
                    scheduler.ctx.transition(task_id, "failed", reason="Argus rejected the current artifact",
                                             actor="Zeus", verification="failed")
                return "rejected", "independent report is blocked, incomplete or stale"
            self.executor.record_review(attempt_id, record)
            scheduler.ctx.transition(task_id, "accepted", reason=f"verified evidence and {record.review.review_id}",
                                     actor="Zeus", actor_type="program", verification="passed")
            scheduler.reconcile()
            return "accepted", record.review.review_id

    @staticmethod
    def _target(receipt):
        artifact = receipt.artifacts[0]
        return Ref(object_id=artifact.artifact_id, version=receipt.generation,
                   content_hash=artifact.content_hash)

    def _restore_report(self, task, receipt, record):
        target = self._target(receipt)
        if (record.review.target != target or record.submission.target != target or
                record.review.review_id != record.submission.review_id or
                record.review.verdict != record.submission.verdict):
            raise ValueError("persisted independent report has inconsistent version or verdict")
        return self.reviewer.common.validate_and_record(
            record.submission, current_target=target, expected_author=task.executor,
            current_inputs=receipt.artifacts[0].inputs, current_artifact_hash=target.content_hash)

    def _report_passes(self, task, receipt, record):
        artifact, target = receipt.artifacts[0], self._target(receipt)
        submission = record.submission
        return (record.target_kind == "TASK_ARTIFACT" and submission.reviewer_role == "Argus" and
                submission.author == task.executor and submission.input_refs == artifact.inputs and
                submission.artifact_ref == target and
                set(task.checks + ["argus:semantic"]) <= set(submission.passed_checks) and
                self.reviewer.common.verify_for_acceptance(record.review.review_id, current_target=target))

    def _review_requests(self, receipt):
        return [entry for entry in self.reviewer.runner.ledger.entries()
                if entry.run_id == receipt.run_id and entry.task_id == receipt.task_id and
                entry.generation == receipt.generation and entry.role_id == "Argus"]

    def project(self, task_id: str, attempt_id: str | None = None) -> dict:
        """P5/P6 read endpoint: preserve authoritative task state and full review evidence."""
        with self.scheduler._lock:
            attempt_id = attempt_id or self.scheduler.attempt_for(task_id)
            if attempt_id is None:
                raise ValueError("task has not been dispatched")
            receipt = self.executor.query_receipt(attempt_id)
            if receipt.task_id != task_id or receipt.run_id != self.scheduler.ctx.run.run_id:
                raise ValueError("execution does not belong to this run/task")
            runtime = self.scheduler.ctx.runtime(task_id)
            return {
                "run_id": receipt.run_id, "task_id": task_id,
                "is_current": (runtime.generation == receipt.generation and
                               self.scheduler.attempt_for(task_id) == attempt_id),
                "runtime": runtime.model_dump(mode="json"),
                "execution": receipt.model_dump(mode="json"),
                "review_requests": [self.reviewer.runner.ledger.query(e.request_id).model_dump(mode="json")
                                    for e in self._review_requests(receipt)],
            }

    def _validate_receipt(self, task, receipt, *, history=False):
        if receipt.run_id != self.scheduler.ctx.run.run_id:
            raise ValueError("execution belongs to a different run")
        if self.scheduler.ctx.run.status not in {"active", "paused"} and not (
                history and self.scheduler.ctx.run.status == "completed"):
            raise ValueError("run cannot accept results in its current state")
        if task.content_hash != receipt.task_hash or set(receipt.checks_passed) != set(task.checks):
            raise ValueError("task/check contract mismatch")
        if set(a.artifact_id for a in receipt.artifacts) != set(task.outputs):
            raise ValueError("artifact outputs do not match the task contract")
        inputs = self.executor._inputs(task.task_id)
        expected_hash = fingerprint({"files": receipt.file_hashes, "task": task.content_hash,
                                     "inputs": [r.model_dump(mode="json") for r in inputs],
                                     "acceptance": receipt.acceptance_hash})
        for artifact in receipt.artifacts:
            if (artifact.inputs != inputs or artifact.task_id != task.task_id or
                    artifact.attempt_id != receipt.attempt_id or
                    artifact.run_id != self.scheduler.ctx.run.run_id or
                    artifact.test_version != receipt.acceptance_hash or
                    artifact.content_hash != expected_hash or
                    artifact.implementation_version != fingerprint(receipt.file_hashes)):
                raise ValueError("artifact/input/version binding mismatch")

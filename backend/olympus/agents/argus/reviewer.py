from olympus.agents.momus.design.reviewer import semantic_findings
from olympus.agents.runtime import AgentRunner, RolePermissions, RoleRunInput
from olympus.agents.runtime.models import RunnerBudget
from olympus.contracts.models import Ref, Task, fingerprint
from olympus.reviews.common import CommonReviewer, ReviewFinding, ReviewSubmission


class ArgusReviewer:
    def __init__(self, runner: AgentRunner, common: CommonReviewer | None = None):
        self.runner = runner
        self.common = common if common is not None else CommonReviewer()

    def review(self, task: Task, receipt, *, actual_files: dict[str, str], budget=None):
        if receipt.state != "succeeded" or set(receipt.checks_passed) != set(task.checks):
            raise ValueError("Argus requires actual passed program checks")
        artifact = receipt.artifacts[0]
        target = Ref(object_id=artifact.artifact_id, version=receipt.generation,
                     content_hash=artifact.content_hash)
        manifest = {
            "task": task.model_dump(mode="json"), "artifact": target.model_dump(mode="json"),
            "inputs": [r.model_dump(mode="json") for r in artifact.inputs],
            "files": actual_files, "check_evidence": receipt.check_outputs,
            "prompt": "Argus: independently review the actual artifact and test evidence against the task. "
                      "Return verdict and findings [{code,severity,message}]. Do not trust author claims, "
                      "change code, change acceptance or write runtime status.",
        }
        result = self.runner.run(RoleRunInput(
            role_id="Argus", task_kind="artifact_review", task_id=task.task_id,
            logical_task_id=f"argus-{task.task_id}-g{receipt.generation}-{target.content_hash[:12]}",
            run_id=artifact.run_id, generation=receipt.generation,
            idempotency_key="p4-" + fingerprint({"run": artifact.run_id, "role": "Argus",
                "task": task.content_hash, "target": target.model_dump(mode="json"),
                "inputs": [ref.model_dump(mode="json") for ref in artifact.inputs]}),
            input_manifest=manifest, input_refs=[*artifact.inputs, target],
            permissions=RolePermissions(role_id="Argus", allowed_task_kinds=["artifact_review"],
                                        allowed_input_keys=list(manifest)),
            budget=budget or RunnerBudget(),
        ), output_validator=lambda out: semantic_findings(out, target))
        findings = semantic_findings(result.structured_output, target)
        verdict = "blocked" if any(f.severity == "blocker" for f in findings) else (
            "needs_revision" if findings else "passed")
        review_id = f"ARG-{task.task_id[:70]}-g{receipt.generation}-{target.content_hash[:12]}"
        checks = [*task.checks, "argus:semantic"]
        submission = ReviewSubmission(
            review_id=review_id, target=target, author=task.executor, reviewer="Argus",
            reviewer_role="Argus", target_kind="TASK_ARTIFACT", verdict=verdict,
            findings=[ReviewFinding(finding_id=f"{review_id}-{i}", target=target, category=f.code,
                                    severity=f.severity, message=f.message, raised_by="Argus")
                      for i, f in enumerate(findings)],
            evidence=[receipt.evidence_id, result.execution_record_id], input_refs=artifact.inputs,
            artifact_ref=target, required_checks=checks,
            passed_checks=checks if verdict == "passed" else receipt.checks_passed, mode=result.mode,
        )
        return self.common.validate_and_record(
            submission, current_target=target, expected_author=task.executor,
            current_inputs=artifact.inputs, current_artifact_hash=artifact.content_hash,
        )

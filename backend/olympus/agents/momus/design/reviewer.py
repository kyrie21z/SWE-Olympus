"""Design-specific deterministic checks plus a genuinely separate Momus role call."""

import json
import re
from typing import Literal

from pydantic import Field

from olympus.agents.runtime import AgentRunner, OutputSchemaError, RolePermissions, RoleRunInput
from olympus.agents.runtime.models import RunnerBudget
from olympus.contracts.models import Contract, Design, Id, Ref, Requirement, fingerprint
from olympus.planning.validator import forbidden_terms
from olympus.reviews.common import CommonReviewer, ReviewFinding, ReviewSubmission


class FindingDraft(Contract):
    code: Id
    severity: Literal["blocker", "important"] = "blocker"
    message: str = Field(min_length=1)


def semantic_findings(output: dict, target: Ref) -> list[FindingDraft]:
    """Used by P4's two thin roles; does not replace CommonReviewer."""
    try:
        if output.get("verdict") not in {"passed", "blocked", "needs_revision"}:
            raise ValueError("missing review verdict")
        if "target" in output and Ref.model_validate(output["target"]) != target:
            raise ValueError("stale review target")
        if not isinstance(output.get("findings"), list):
            raise ValueError("findings must be a list")
        findings = [FindingDraft.model_validate(f) for f in output["findings"]]
        if output["verdict"] != "passed" and not findings:
            raise ValueError("a non-passed verdict needs a reason")
        return findings
    except (ValueError, TypeError) as exc:
        raise OutputSchemaError(f"invalid independent review: {exc}") from exc


class DesignReviewer:
    def __init__(self, runner: AgentRunner, common: CommonReviewer | None = None,
                 *, profile: Literal["generic", "campus-v1.3"] = "generic"):
        self.runner = runner
        self.common = common if common is not None else CommonReviewer()
        if profile not in {"generic", "campus-v1.3"}:
            raise ValueError("unknown design validation profile")
        self.profile = profile

    def program_findings(self, req: Requirement, ds: Design) -> list[FindingDraft]:
        issues = []
        def add(code, message):
            issues.append(FindingDraft(code=code, message=message))
        if ds.requirement != req.ref():
            add("STALE_REQUIREMENT", "design references a different requirement version")
        elements = ds.content["elements"]
        ids = [e["id"] for e in elements]
        if len(set(ids)) != len(ids):
            add("DUPLICATE_ELEMENT", "duplicate design element id")
        expected = {r["id"] for r in req.content["requirements"]}
        covered = {s for e in elements for s in e["sources"]}
        if covered - expected:
            add("UNKNOWN_SOURCE", "design cites unknown requirements")
        if expected - covered:
            add("UNCOVERED_REQUIREMENT", "uncovered requirements: " + ",".join(sorted(expected-covered)))
        if any(not ds.content[k] for k in ds.content if k != "exceptions"):
            add("EMPTY_SECTION", "required design section is empty")
        text = json.dumps(ds.content, ensure_ascii=False)
        for term in forbidden_terms(req):
            # Explicit negative scope statements are not implementations of forbidden features.
            positive = re.sub(r"(?:不提供|不实现|不支持|不使用|禁止|排除|不|无)\s*" + re.escape(term), "", text)
            if term in positive:
                add("OUT_OF_SCOPE", f"design introduces out-of-scope term: {term}")
        if self.profile == "campus-v1.3":
            if not any("递补" in r["text"] for r in req.content["requirements"]):
                add("PROFILE_MISMATCH", "campus profile requires the declared campus baseline")
            rules = [
                ("UNIQUE_ACCOUNT", "唯一" in text or "unique" in text.lower()),
                ("THREE_STATES", {"CONFIRMED", "WAITLISTED", "CANCELLED"} <= set(ds.content["states"])),
                ("TRANSACTION", "事务" in ds.content["concurrency"] or "transaction" in ds.content["concurrency"].lower()),
                ("AUTO_PROMOTION", "自动递补" in text),
                ("CANCEL_QUEUE", bool(re.search(r"跳过取消|排除取消|退出有效|取消.{0,12}有效队列", text))),
                ("ACTIVE_QUERY", "主动查询" in ds.content["interaction"]),
            ]
            for code, ok in rules:
                if not ok:
                    add(code, "missing campus acceptance rule: " + code)
        return issues

    def review(self, req: Requirement, ds: Design, *, run_id="RUN-design"):
        # Revalidate program-owned objects instead of trusting model_copy updates.
        ds = Design.model_validate(ds.model_dump(mode="json"))
        findings = self.program_findings(req, ds)
        manifest = {
            "requirement": req.ref().model_dump(mode="json"), "requirement_content": req.content,
            "design": ds.ref().model_dump(mode="json"), "design_content": ds.content,
            "prompt": "Momus: independently check meaning, omissions and scope. Return verdict and findings "
                      "[{code,severity,message}]. Do not read or invent author self-evaluation. "
                      "A passed statement cannot override missing requirements or program findings.",
        }
        request = RoleRunInput(
            role_id="Momus", task_kind="design_review", task_id=ds.object_id,
            logical_task_id=f"review-{ds.object_id[:85]}-v{ds.version}-{ds.content_hash[:10]}",
            run_id=run_id, generation=ds.version, input_manifest=manifest, input_refs=[req.ref(), ds.ref()],
            idempotency_key="p4-" + fingerprint({"run": run_id, "role": "Momus-design",
                "requirement": req.ref().model_dump(mode="json"), "design": ds.ref().model_dump(mode="json")}),
            permissions=RolePermissions(role_id="Momus", allowed_task_kinds=["design_review"],
                                        allowed_input_keys=list(manifest)),
            budget=RunnerBudget(max_calls=30, calls_used=self.runner.ledger.total_calls_accounted(run_id)),
        )
        result = self.runner.run(request, output_validator=lambda out: semantic_findings(out, ds.ref()))
        findings += semantic_findings(result.structured_output, ds.ref())
        verdict = "blocked" if any(f.severity == "blocker" for f in findings) else (
            "needs_revision" if findings else "passed")
        checks = ["design:program", "momus:design-semantic"]
        report_id = f"REV-{ds.object_id[:85]}-v{ds.version}-{ds.content_hash[:10]}"
        submission = ReviewSubmission(
            review_id=report_id, target=ds.ref(), author="Athena", reviewer="Momus",
            reviewer_role="Momus", target_kind="DS", verdict=verdict,
            findings=[ReviewFinding(finding_id=f"{report_id}-{i}", target=ds.ref(), category=f.code,
                                    severity=f.severity, message=f.message, raised_by="Momus")
                      for i, f in enumerate(findings)],
            evidence=["program:design-check", result.execution_record_id], input_refs=[req.ref()],
            required_checks=checks, passed_checks=checks if verdict == "passed" else [], mode=result.mode,
        )
        return self.common.validate_and_record(submission, current_target=ds.ref(),
                                               expected_author="Athena", current_inputs=[req.ref()])

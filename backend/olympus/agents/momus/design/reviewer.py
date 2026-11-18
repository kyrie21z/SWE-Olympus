"""Design-specific deterministic checks plus a genuinely separate Momus role call."""

import json
import re
from typing import Literal

from pydantic import Field

from olympus.agents.runtime import AgentRunner, OutputSchemaError, RolePermissions, RoleRunInput
from olympus.agents.runtime.models import RunnerBudget
from olympus.contracts.models import Contract, Design, Id, Ref, Requirement, fingerprint
from olympus.planning.validator import forbidden_terms
from olympus.reviews.common import CommonReviewer, ReviewFinding, ReviewRecord, ReviewSubmission


class FindingDraft(Contract):
    code: Id
    severity: Literal["blocker", "important"] = "blocker"
    message: str = Field(min_length=1)


_FINDING_CHECK = "program:verify-design-finding-quotes"
_DESIGN_CHECKS = ["design:program", "momus:design-semantic"]
_RECHECK_SCHEMA = {
    "type": "object", "required": ["decisions"], "additionalProperties": False,
    "properties": {"decisions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["finding_id", "decision", "requirement_id", "section", "element_id", "quote"],
        "properties": {
            "finding_id": {"type": "string"},
            "decision": {"enum": ["keep", "already_covered"]},
            "requirement_id": {"type": ["string", "null"]},
            "section": {"enum": ["description", "acceptance_method", None]},
            "element_id": {"type": ["string", "null"]},
            "quote": {"type": ["string", "null"]},
        },
    }}},
}


def review_output_schema(finding_type=FindingDraft):
    finding = finding_type.model_json_schema()
    finding["properties"].pop("schema_version", None)
    return {
        "type": "object", "additionalProperties": False,
        "required": ["verdict", "findings"], "properties": {
            "verdict": {"type": "string", "enum": ["passed", "blocked", "needs_revision"]},
            "findings": {"type": "array", "items": finding},
        },
    }


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
        program = self.program_findings(req, ds)
        schema = review_output_schema()
        manifest = {
            "requirement": req.ref().model_dump(mode="json"), "requirement_content": req.content,
            "design": ds.ref().model_dump(mode="json"), "design_content": ds.content,
            "output_schema": schema,
            "prompt": "Momus: independently check meaning, omissions and scope. Return verdict and findings "
                      "[{code,severity,message}]. Read every related element's description and acceptance_method before "
                      "claiming an omission; recognize equivalent wording and combine evidence across elements. "
                      "Name applicable requirement IDs and the concrete missing behavior. This is a design review; "
                      "judge behavior and the stated method of acceptance, while executable checks are frozen after planning. "
                      "Do not require code or a particular test layout. Report only issues grounded in the approved requirement. "
                      "Do not read or invent author self-evaluation.",
        }
        request = RoleRunInput(
            role_id="Momus", task_kind="design_review", task_id=ds.object_id,
            logical_task_id=f"review-{ds.object_id[:85]}-v{ds.version}-{ds.content_hash[:10]}",
            run_id=run_id, generation=ds.version, input_manifest=manifest, input_refs=[req.ref(), ds.ref()],
            output_schema=schema,
            idempotency_key="p4-" + fingerprint({"run": run_id, "role": "Momus-design",
                "requirement": req.ref().model_dump(mode="json"), "design": ds.ref().model_dump(mode="json")}),
            permissions=RolePermissions(role_id="Momus", allowed_task_kinds=["design_review"],
                                        allowed_input_keys=list(manifest)),
            budget=RunnerBudget(max_calls=30, calls_used=self.runner.ledger.total_calls_accounted(run_id)),
        )
        result = self.runner.run(request, output_validator=lambda out: semantic_findings(out, ds.ref()))
        findings = [(f, "program") for f in program] + [
            (f, "Momus") for f in semantic_findings(result.structured_output, ds.ref())
        ]
        verdict = _verdict([finding for finding, _ in findings])
        report_id = f"REV-{ds.object_id[:85]}-v{ds.version}-{ds.content_hash[:10]}"
        submission = ReviewSubmission(
            review_id=report_id, target=ds.ref(), author="Athena", reviewer="Momus",
            reviewer_role="Momus", target_kind="DS", verdict=verdict,
            findings=[ReviewFinding(finding_id=f"{report_id}-{i}", target=ds.ref(), category=f.code,
                                    severity=f.severity, message=f.message, raised_by=origin)
                      for i, (f, origin) in enumerate(findings)],
            evidence=["program:design-check", result.execution_record_id], input_refs=[req.ref()],
            required_checks=_DESIGN_CHECKS, passed_checks=_DESIGN_CHECKS if verdict == "passed" else [], mode=result.mode,
        )
        return self.common.validate_and_record(submission, current_target=ds.ref(),
                                               expected_author="Athena", current_inputs=[req.ref()])

    def recheck(self, req: Requirement, ds: Design, report: ReviewRecord, *, run_id: str) -> ReviewRecord:
        """Challenge reported omissions against the unchanged design and preserve every decision."""
        original = report.submission
        if original.target != ds.ref() or original.input_refs != [req.ref()] or original.reviewer != "Momus":
            raise ValueError("finding recheck requires the same reviewed design and requirement")
        if self.runner.provider.mode != "real_model" or _FINDING_CHECK in original.evidence:
            return report
        program = {(f.code, f.message) for f in self.program_findings(req, ds)}
        candidates = {f.finding_id: f for f in original.findings
                      if f.raised_by == "Momus" and f.status == "open" and (f.category, f.message) not in program}
        if not candidates:
            return report

        manifest = {
            "requirement": req.content, "design": ds.content,
            "findings": [{"finding_id": f.finding_id, "message": f.message} for f in candidates.values()],
            "output_schema": _RECHECK_SCHEMA,
            "prompt": "Re-evaluate each earlier finding against the exact current design, especially acceptance_method. "
                      "Look for affirmative counterevidence to a claimed omission. Both design behavior and any acceptance "
                      "case mentioned in the finding must already be present. Use already_covered only when the complete "
                      "claimed omission is contradicted, with the requirement ID, linked element, field and a verbatim quote "
                      "sufficient to show it. Keep partially addressed or unverified findings. Return one decision per finding. "
                      "For keep use null citation fields. No new issues, no changes to the design. Return JSON.",
        }

        def decisions(output):
            rows = output.get("decisions")
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise OutputSchemaError("finding recheck must return decisions")
            ids = [row.get("finding_id") for row in rows]
            if (not all(isinstance(key, str) for key in ids) or len(ids) != len(candidates) or
                    set(ids) != set(candidates) or any(row.get("decision") not in ("keep", "already_covered") for row in rows)):
                raise OutputSchemaError("finding recheck must address each supplied finding exactly once",
                                        hint="为每条原审查意见返回一个复核判断，保留原 finding_id。")
            return rows

        key = fingerprint({"review": original.review_id, "target": ds.ref().model_dump(mode="json")})
        result = self.runner.run(RoleRunInput(
            role_id="Momus", task_kind="design_finding_check", task_id=ds.object_id,
            logical_task_id=f"design-finding-check-{key}", run_id=run_id, generation=ds.version,
            input_manifest=manifest, input_refs=[req.ref(), ds.ref()], output_schema=_RECHECK_SCHEMA,
            idempotency_key=f"design-finding-check-{key}",
            permissions=RolePermissions(role_id="Momus", allowed_task_kinds=["design_finding_check"],
                                        allowed_input_keys=list(manifest)),
            budget=RunnerBudget(max_calls=30, calls_used=self.runner.ledger.total_calls_accounted(run_id)),
        ), output_validator=decisions)
        by_id = {e["id"]: e for e in ds.content["elements"]}
        requirement_ids = {row["id"] for row in req.content["requirements"]}
        findings = [f.model_copy(deep=True) for f in original.findings]
        copied_by_id = {finding.finding_id: finding for finding in findings}
        for row in decisions(result.structured_output):
            if row["decision"] != "already_covered":
                continue
            element_id = row.get("element_id")
            element = by_id.get(element_id) if isinstance(element_id, str) else None
            section, quote = row.get("section"), row.get("quote")
            source = row.get("requirement_id")
            finding = copied_by_id[row["finding_id"]]
            text = f"{finding.category} {finding.message}"
            mentioned = {rid for rid in requirement_ids if re.search(
                rf"(?<![A-Za-z0-9_-]){re.escape(rid)}(?![A-Za-z0-9_-])", text,
            )}
            if (not isinstance(source, str) or source not in requirement_ids or
                    (mentioned and mentioned != {source}) or not element or source not in element["sources"] or
                    section not in ("description", "acceptance_method") or not isinstance(quote, str) or
                    len(quote.strip()) < 12 or quote not in element[section]):
                continue
            finding.status, finding.closed_by = "closed", "Momus"
            finding.closure_reason = f"{source} · {element_id} · {section}：{quote}"
        verdict = _verdict([f for f in findings if f.status == "open"])
        checked = original.model_copy(update={
            "review_id": f"{original.review_id[:130]}-CHK-{key[:10]}", "verdict": verdict, "findings": findings,
            "evidence": [*original.evidence, _FINDING_CHECK, result.execution_record_id],
            "passed_checks": _DESIGN_CHECKS if verdict == "passed" else [],
        })
        return self.common.validate_and_record(checked, current_target=ds.ref(),
                                               expected_author="Athena", current_inputs=[req.ref()])


def _verdict(findings):
    if any(f.severity == "blocker" for f in findings):
        return "blocked"
    return "needs_revision" if findings else "passed"

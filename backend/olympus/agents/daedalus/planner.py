"""Daedalus: materialize versioned plans from semantic proposals or an explicit fixture."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Protocol

from pydantic import JsonValue, ValidationError, create_model

from olympus.agents.runtime import AgentRunner, OutputSchemaError, RolePermissions, RoleRunInput
from olympus.contracts.models import Contract, Design, Plan, Provenance, Ref, Requirement, Task, fingerprint
from olympus.planning.models import TaskCard, build_task_cards
from olympus.planning.validator import design_element_ids, requirement_ids, validate_plan_semantics

DAEDALUS = "Daedalus"
PLAN_TASK_KIND = "plan"
ROOT = Path(__file__).resolve().parents[4]
# Share Task's field types and defaults, without its version/hash validator.
# The proposal is normalized first, then the canonical Task validates invariants.
TaskProposal = create_model(
    "TaskProposal",
    __base__=Contract,
    **{
        name: (field.annotation, deepcopy(field))
        for name, field in Task.model_fields.items()
        if name not in {"schema_version", "version", "content_hash"}
    },
)

_FLAGGED_CODES = (
    "UNCOVERED_REQUIREMENT",
    "UNCOVERED_DESIGN",
    "MISSING_TEST_COVERAGE",
    "MISSING_INPUT_SUPPLY",
    "INVALID_WRITE_SCOPE",
)
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class PlanRunner(Protocol):
    def run(self, request: dict[str, JsonValue]) -> Plan | dict[str, JsonValue]: ...


def _normalize_scope(scope: str) -> str:
    normalized = scope.replace("\\", "/")
    if _DRIVE_PREFIX.match(normalized):
        normalized = normalized[2:]
    parts = [part for part in normalized.split("/") if part not in ("", ".", "..")]
    return "/".join(parts) or "packages"


class DaedalusPlanner:
    role_id = DAEDALUS
    task_kind = PLAN_TASK_KIND

    def __init__(self, runner: PlanRunner | AgentRunner | None = None):
        self._runner = runner

    @staticmethod
    def draft_schema() -> dict[str, JsonValue]:
        task = TaskProposal.model_json_schema()
        for key in ("version", "content_hash", "schema_version"):
            task["properties"].pop(key, None)
        task["required"] = [key for key in task["required"] if key not in ("version", "content_hash")]
        definitions = task.pop("$defs", {})
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["tasks"],
            "properties": {"tasks": {"type": "array", "minItems": 1, "items": task}},
            "$defs": definitions,
        }

    def build_prompt(self, req: Requirement, ds: Design) -> str:
        return "\n".join(
            [
                "角色：Daedalus（任务分解）。依据提供的完整需求和设计正文规划，业务领域不限。",
                "为每条需求及设计元素建立可验证任务；任务数和依赖由实际内容决定。",
                "遵守范围外限制、接口先行、执行者与复核者分离，每条需求有测试任务覆盖。",
                "仅输出 JSON：tasks 数组，任务字段遵循 output_schema，不输出状态或确认。",
                "版本、指纹、输入绑定由程序生成；不要推算哈希。write_scope 仅为后续任务范围建议。",
                f"当前需求 {req.object_id} v{req.version}；当前设计 {ds.object_id} v{ds.version}。",
            ]
        )

    def build_input_manifest(self, req: Requirement, ds: Design) -> dict[str, JsonValue]:
        return {
            "role_id": self.role_id,
            "task_kind": self.task_kind,
            "prompt": self.build_prompt(req, ds),
            "requirement": req.ref().model_dump(mode="json"),
            "design": ds.ref().model_dump(mode="json"),
            "requirement_content": req.content,
            "design_content": ds.content,
            "requirement_ids": requirement_ids(req),
            "design_element_ids": design_element_ids(ds),
            "out_of_scope": list(req.content["out_of_scope"]),
            "output_schema": self.draft_schema(),
            "constraints": [
                "executor != reviewer",
                "every requirement covered by a test task",
                "no accepted state or execution",
            ],
        }

    def build_plan(self, req: Requirement, ds: Design) -> Plan:
        """No-provider adapter: only the exact versioned course fixture is available."""
        fixture = json.loads((ROOT / "fixtures/contracts/valid.json").read_text(encoding="utf-8"))
        plan = Plan.model_validate(fixture["plan"])
        if req.ref() != plan.requirement or ds.ref() != plan.design or ds.requirement != req.ref():
            raise OutputSchemaError(
                "No planning provider: the default adapter only supports the bound fixture"
            )
        return plan

    @staticmethod
    def _materialize(output: dict[str, JsonValue], req: Requirement, ds: Design, mode: str) -> Plan:
        raw_tasks = output.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise OutputSchemaError("plan output requires a non-empty tasks array; no fixture fallback")
        try:
            for key, expected in (("requirement", req.ref()), ("design", ds.ref())):
                if key in output and Ref.model_validate(output[key]) != expected:
                    raise ValueError(f"plan output has stale {key} reference")
            tasks = []
            for raw in raw_tasks:
                if not isinstance(raw, dict):
                    raise ValueError("each task must be an object")
                # Models propose semantic fields. Program code owns all new version/hash values.
                data = {
                    key: value
                    for key, value in raw.items()
                    if key not in {"schema_version", "version", "content_hash"}
                }
                data = TaskProposal.model_validate(data).model_dump(mode="json", exclude={"schema_version"})
                tasks.append(Task(**data, version=1, content_hash=fingerprint(data)))
            content = {
                "tasks": [
                    {"task_id": task.task_id, "version": task.version, "content_hash": task.content_hash}
                    for task in tasks
                ]
            }
            return Plan(
                object_id=f"PLAN-{req.object_id}",
                version=1,
                content=content,
                content_hash=fingerprint(content),
                requirement=req.ref(),
                design=ds.ref(),
                tasks=tasks,
                provenance=[
                    Provenance(
                        kind="synthetic_fixture" if mode == "mock" else "agent_hypothesis",
                        source_id=DAEDALUS,
                        locator="Versioned task proposal; input refs and fingerprints bound by program",
                    )
                ],
            )
        except (ValidationError, ValueError, TypeError) as exc:
            raise OutputSchemaError(f"invalid plan proposal: {exc}") from exc

    def _plan_via_runner(self, req: Requirement, ds: Design, *, previous: Plan | None = None,
                         findings: list[str] | None = None) -> Plan:
        manifest = self.build_input_manifest(req, ds)
        refs = [req.ref(), ds.ref()]
        if previous is not None:
            refs.append(previous.ref())
            manifest["previous_tasks"] = [task.semantic_content() for task in previous.tasks]
            manifest["review_findings"] = findings or []
            manifest["prompt"] += "\n逐项处理 review_findings，返回修订后的全部任务。保留未受影响任务的 ID。"
        run_input = RoleRunInput(
            role_id=self.role_id,
            task_kind=self.task_kind,
            logical_task_id=f"LOG-PLAN-{req.object_id}",
            task_id=f"PLAN-{req.object_id}",
            input_manifest=manifest,
            input_refs=refs,
            output_schema=self.draft_schema(),
            permissions=RolePermissions(
                role_id=self.role_id,
                allowed_task_kinds=[self.task_kind],
                allowed_input_keys=list(manifest),
                can_propose_task_scopes=True,
            ),
            fixture_key="Daedalus:plan",
        )
        mode = self._runner.provider.mode
        role_result = self._runner.run(
            run_input, output_validator=lambda value: self._materialize(value, req, ds, mode)
        )
        return self._materialize(role_result.structured_output, req, ds, mode)

    def plan(self, req: Requirement, ds: Design) -> Plan:
        if ds.requirement != req.ref():
            raise OutputSchemaError("design does not bind the supplied requirement version/hash")
        if self._runner is None:
            return self.build_plan(req, ds)
        if isinstance(self._runner, AgentRunner):
            return self._plan_via_runner(req, ds)
        result = self._runner.run(self.build_input_manifest(req, ds))
        output = result.model_dump(mode="json") if isinstance(result, Plan) else result
        return self._materialize(output, req, ds, "mock")

    def build_task_cards(self, req: Requirement, ds: Design, plan: Plan) -> list[TaskCard]:
        return build_task_cards(plan, req, ds)

    def revise_plan(self, plan: Plan, findings: list[str], req: Requirement, ds: Design) -> Plan:
        """Apply one deterministic revision that repairs programmatic blockers.

        Findings are the review evidence; repairs are recomputed from the current
        plan so the revision is reproducible. Issues that cannot be repaired
        safely (cycles, out-of-scope work, granularity) survive into the revision
        and the workflow halts for a human instead of looping.
        """
        report = validate_plan_semantics(plan, req, ds)
        req_ids = requirement_ids(req)
        ds_ids = design_element_ids(ds)
        known_sources = set(req_ids) | set(ds_ids)
        task_ids = [task.task_id for task in plan.tasks]
        known_tasks = set(task_ids)
        flagged = {code for code in _FLAGGED_CODES if any(code in finding for finding in findings)}
        flagged.update(issue.code for issue in report.issues)

        uncovered_req = [
            issue.source_id
            for issue in report.issues
            if issue.code == "UNCOVERED_REQUIREMENT" and issue.source_id
        ]
        uncovered_ds = [
            issue.source_id for issue in report.issues if issue.code == "UNCOVERED_DESIGN" and issue.source_id
        ]
        missing_test = [
            issue.source_id
            for issue in report.issues
            if issue.code == "MISSING_TEST_COVERAGE" and issue.source_id
        ]
        producers = {output: task.task_id for task in plan.tasks for output in task.outputs}
        interface_id = next((task.task_id for task in plan.tasks if task.type == "interface"), task_ids[0])
        test_id = next((task.task_id for task in plan.tasks if task.type == "test"), None)

        revised: list[Task] = []
        for task in plan.tasks:
            data = task.semantic_content()
            data["depends_on"] = [dep for dep in data["depends_on"] if dep in known_tasks]
            data["sources"] = [source for source in data["sources"] if source in known_sources]
            if not any(source in req_ids for source in data["sources"]):
                data["sources"].append(req_ids[0])
            if not any(source in ds_ids for source in data["sources"]):
                data["sources"].append(ds_ids[0])
            if task.task_id == interface_id:
                for source in uncovered_req + uncovered_ds:
                    if source not in data["sources"]:
                        data["sources"].append(source)
            if test_id is not None and task.task_id == test_id:
                for source in missing_test:
                    if source not in data["sources"]:
                        data["sources"].append(source)
            if "MISSING_INPUT_SUPPLY" in flagged:
                for item in list(data["inputs"]):
                    if not item.startswith("artifact:"):
                        continue
                    producer = producers.get(item)
                    if producer is None:
                        data["inputs"].remove(item)
                    elif producer not in data["depends_on"]:
                        data["depends_on"].append(producer)
            if "INVALID_WRITE_SCOPE" in flagged:
                data["write_scope"] = [_normalize_scope(scope) for scope in data["write_scope"]]
            data["sources"] = list(dict.fromkeys(data["sources"]))
            data["depends_on"] = list(dict.fromkeys(data["depends_on"]))
            if data == task.semantic_content():
                revised.append(task)
            else:
                revised.append(Task(**data, version=task.version + 1, content_hash=fingerprint(data)))

        content = {
            "tasks": [
                {"task_id": task.task_id, "version": task.version, "content_hash": task.content_hash}
                for task in revised
            ]
        }
        return Plan(
            object_id=plan.object_id,
            version=plan.version + 1,
            content=content,
            content_hash=fingerprint(content),
            provenance=[
                *plan.provenance,
                Provenance(
                    kind="agent_hypothesis",
                    source_id=DAEDALUS,
                    locator="单次修订：依据 Momus 审查发现修复计划",
                ),
            ],
            requirement=plan.requirement,
            design=plan.design,
            tasks=revised,
        )

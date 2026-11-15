"""Daedalus planner: deterministic plan generation with an optional runner hook.

Daedalus produces the plan and at most one revision. It never accepts tasks,
never executes them and never closes its own review blockers.
"""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import JsonValue

from olympus.contracts.models import (
    Design,
    Plan,
    Provenance,
    Requirement,
    Task,
    fingerprint,
)
from olympus.planning.models import TaskCard, build_task_cards
from olympus.planning.validator import (
    design_element_ids,
    requirement_ids,
    validate_plan_semantics,
)

DAEDALUS = "Daedalus"
PLAN_TASK_KIND = "plan"
REVIEWER = "Argus"


def _synthetic_provenance() -> Provenance:
    return Provenance(
        kind="synthetic_fixture", source_id="H-01", locator="人工构造案例，非真实用户决定"
    )


# Standard campus registration decomposition: 8 tasks, 12 dependency edges.
# ``fr`` holds 1-based requirement ordinals; ``ds_anchor`` selects the design
# element by the requirement it sources, so the same blueprint also applies to
# equivalent requirement/design pairs.
TASK_BLUEPRINT: tuple[dict, ...] = (
    {
        "type": "interface",
        "goal": "接口类型与测试夹具",
        "fr": (1, 2, 3, 4, 5),
        "deps": (),
        "executor": "Hephaestus",
        "ds_anchor": 3,
    },
    {
        "type": "implement",
        "goal": "报名与取消服务",
        "fr": (1, 2, 4),
        "deps": ("T01",),
        "executor": "Hephaestus",
        "ds_anchor": 3,
    },
    {
        "type": "implement",
        "goal": "候补与自动递补服务",
        "fr": (3, 4),
        "deps": ("T01",),
        "executor": "Hephaestus",
        "ds_anchor": 3,
    },
    {
        "type": "implement",
        "goal": "报名与恢复交互",
        "fr": (1, 2, 4),
        "deps": ("T01",),
        "executor": "Hephaestus",
        "ds_anchor": 5,
    },
    {
        "type": "implement",
        "goal": "状态查询页面",
        "fr": (5,),
        "deps": ("T03",),
        "executor": "Hephaestus",
        "ds_anchor": 5,
    },
    {
        "type": "test",
        "goal": "边界与并发测试",
        "fr": (1, 2, 3, 4, 5),
        "deps": ("T02", "T03"),
        "executor": "Momus",
        "ds_anchor": 3,
    },
    {
        "type": "integrate",
        "goal": "联调与恢复走查",
        "fr": (1, 2, 3, 4, 5),
        "deps": ("T02", "T03", "T04", "T05"),
        "executor": "Hermes",
        "ds_anchor": 3,
    },
    {
        "type": "doc",
        "goal": "验收汇总与交付清单",
        "fr": (1, 2, 3, 4, 5),
        "deps": ("T06", "T07"),
        "executor": "Hermes",
        "ds_anchor": 3,
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
    """Optional P2 runner: a plan request in, plan JSON out."""

    def run(self, request: dict[str, JsonValue]) -> Plan | dict[str, JsonValue]: ...


def _ds_element_for(ds: Design, req_ids: list[str], anchor: int) -> str:
    """Design element that sources the anchored requirement, with a positional fallback."""
    target = req_ids[min(anchor, len(req_ids)) - 1]
    for element in ds.content["elements"]:
        if target in element["sources"]:
            return element["id"]
    ds_ids = design_element_ids(ds)
    return ds_ids[min(anchor, len(ds_ids)) - 1]


def _normalize_scope(scope: str) -> str:
    normalized = scope.replace("\\", "/")
    if _DRIVE_PREFIX.match(normalized):
        normalized = normalized[2:]
    parts = [part for part in normalized.split("/") if part not in ("", ".", "..")]
    return "/".join(parts) or "packages"


class DaedalusPlanner:
    role_id = DAEDALUS
    task_kind = PLAN_TASK_KIND

    def __init__(self, runner: PlanRunner | None = None):
        self._runner = runner

    def build_prompt(self, req: Requirement, ds: Design) -> str:
        out_of_scope = "、".join(req.content["out_of_scope"]) or "无"
        return "\n".join(
            [
                f"角色：{self.role_id}（任务分解）",
                f"任务类型：{self.task_kind}",
                f"需求：{req.object_id} v{req.version}（{req.content_hash[:12]}）",
                f"设计：{ds.object_id} v{ds.version}（{ds.content_hash[:12]}）",
                "要求：为每条 FR 与每个设计元素建立可验证任务；测试任务必须覆盖全部 FR。",
                "接口先行：先产出接口契约与测试夹具，再并行实现；执行者不得自审。",
                f"范围外（不得出现在目标、来源、输出、写入范围或检查中）：{out_of_scope}",
                "输出：八任务十二边的 PLAN 语义内容；禁止标记 accepted 或触发执行。",
                "修订：若收到审查阻塞项，只允许修订一次并重新计算版本与内容指纹。",
            ]
        )

    def build_input_manifest(self, req: Requirement, ds: Design) -> dict[str, JsonValue]:
        return {
            "role_id": self.role_id,
            "task_kind": self.task_kind,
            "prompt": self.build_prompt(req, ds),
            "requirement": req.ref().model_dump(mode="json"),
            "design": ds.ref().model_dump(mode="json"),
            "requirement_ids": requirement_ids(req),
            "design_element_ids": design_element_ids(ds),
            "out_of_scope": list(req.content["out_of_scope"]),
            "constraints": [
                "executor != reviewer",
                "every FR covered by a test task",
                "no accepted state or execution",
            ],
        }

    def build_plan(self, req: Requirement, ds: Design) -> Plan:
        req_ids = requirement_ids(req)
        tasks: list[Task] = []
        for index, spec in enumerate(TASK_BLUEPRINT, 1):
            task_id = f"T{index:02d}"
            sources = list(
                dict.fromkeys(
                    [req_ids[min(ordinal, len(req_ids)) - 1] for ordinal in spec["fr"]]
                    + [_ds_element_for(ds, req_ids, spec["ds_anchor"])]
                )
            )
            data = {
                "task_id": task_id,
                "type": spec["type"],
                "goal": spec["goal"],
                "sources": sources,
                "inputs": [f"artifact:{dep}" for dep in spec["deps"]],
                "outputs": [f"artifact:{task_id}"],
                "depends_on": list(spec["deps"]),
                "executor": spec["executor"],
                "reviewer": REVIEWER,
                "write_scope": [f"packages/{task_id.lower()}"],
                "checks": [f"check:{task_id}"],
                "resources": [],
                "priority": 0,
                "policy": {"schema_version": "1.0", "max_auto_retries": 2, "timeout_seconds": 120},
                "required": True,
            }
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
            provenance=[_synthetic_provenance()],
            requirement=req.ref(),
            design=ds.ref(),
            tasks=tasks,
        )

    def plan(self, req: Requirement, ds: Design) -> Plan:
        """Generate a plan through the optional runner, else the deterministic adapter."""
        if self._runner is None:
            return self.build_plan(req, ds)
        result = self._runner.run(self.build_input_manifest(req, ds))
        return result if isinstance(result, Plan) else Plan.model_validate(result)

    def build_task_cards(self, req: Requirement, ds: Design, plan: Plan) -> list[TaskCard]:
        return build_task_cards(plan, req, ds)

    def revise_plan(
        self, plan: Plan, findings: list[str], req: Requirement, ds: Design
    ) -> Plan:
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
        flagged = {
            code for code in _FLAGGED_CODES if any(code in finding for finding in findings)
        }
        flagged.update(issue.code for issue in report.issues)

        uncovered_req = [
            issue.source_id
            for issue in report.issues
            if issue.code == "UNCOVERED_REQUIREMENT" and issue.source_id
        ]
        uncovered_ds = [
            issue.source_id
            for issue in report.issues
            if issue.code == "UNCOVERED_DESIGN" and issue.source_id
        ]
        missing_test = [
            issue.source_id
            for issue in report.issues
            if issue.code == "MISSING_TEST_COVERAGE" and issue.source_id
        ]
        producers = {output: task.task_id for task in plan.tasks for output in task.outputs}
        interface_id = next(
            (task.task_id for task in plan.tasks if task.type == "interface"), task_ids[0]
        )
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
                revised.append(
                    Task(**data, version=task.version + 1, content_hash=fingerprint(data))
                )

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

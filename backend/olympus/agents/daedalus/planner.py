"""Daedalus: materialize versioned plans from semantic proposals or an explicit fixture."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Protocol

from pydantic import JsonValue, ValidationError, create_model, model_validator

from olympus.agents.runtime import AgentRunner, OutputSchemaError, RolePermissions, RoleRunInput
from olympus.contracts.models import Contract, Design, Plan, Provenance, Ref, Requirement, Task, fingerprint, validate_task_api_contracts
from olympus.planning.models import TaskCard, build_task_cards
from olympus.planning.validator import (
    coverage_obligations as delivery_obligations,
    design_covering_tasks,
    design_coverage_task_types,
    design_element_ids,
    requirement_coverage_task_types,
    requirement_covering_tasks,
    requirement_ids,
    validate_plan_semantics,
)

DAEDALUS = "Daedalus"
PLAN_TASK_KIND = "plan"
ROOT = Path(__file__).resolve().parents[4]
# Share Task's field types and defaults, without its version/hash validator.
# The proposal is normalized first, then the canonical Task validates invariants.
@model_validator(mode="after")
def _proposal_api_contracts(proposal):
    validate_task_api_contracts(proposal.api_contracts, proposal.write_scope)
    return proposal


TaskProposal = create_model(
    "TaskProposal",
    __base__=Contract,
    __validators__={"valid_api_contracts": _proposal_api_contracts},
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
_EXACT_OUTPUT_SCOPE_SOURCE = "DAEDALUS-EXACT-OUTPUT-SCOPE"


def _restrict_root_scope(data):
    """Narrow an explicit Python author's root claim; never invent or move files."""
    if (data.get("write_scope") != ["."] or data.get("type") not in ("interface", "implement")
            or data.get("executor") != "Hephaestus"):
        return False
    outputs, contracts = data.get("outputs"), data.get("api_contracts")
    if (not isinstance(outputs, list) or not outputs or not all(isinstance(name, str) for name in outputs)
            or len(outputs) != len(set(outputs)) or not isinstance(contracts, dict) or not contracts
            or not all(isinstance(name, str) and name in outputs for name in contracts)):
        return False
    try:
        for name in outputs:
            if (any(ord(char) < 32 or char in '<>"|?*' for char in name)
                    or any(part != part.strip() or part.endswith(".") for part in name.split("/"))):
                return False
            # The existing contract guard checks canonical, bounded .py paths.
            validate_task_api_contracts({name: {}}, [name])
    except (TypeError, ValueError):
        return False
    data["write_scope"] = list(outputs)
    return True


def _proposal_schema_hints(error: ValidationError, prefix: str) -> list[str]:
    allowed = set(TaskProposal.model_fields) | set(Ref.model_fields) | {"max_auto_retries", "timeout_seconds"}
    messages = {
        "missing": "必填字段(required)", "list_type": "必须是数组(list)，不能为 null",
        "string_type": "必须是字符串(string)", "int_type": "必须是整数(integer)，不能为布尔值或 null",
        "int_parsing": "必须是整数(integer)", "bool_type": "必须是布尔值(boolean)",
        "model_type": "必须是对象(object)", "dict_type": "必须是对象(object)",
        "literal_error": "必须使用字段 schema 允许的枚举值", "extra_forbidden": "不允许额外字段",
        "value_error": "不满足任务编号、依赖唯一性或独立复核等契约约束",
    }
    api_errors = {
        "api_contracts paths must be canonical bounded relative .py files":
            "键必须是规范的有界相对 .py 文件路径，不含绝对路径、反斜线、隐藏目录或遍历",
        "api_contracts file must be inside this task's bounded write_scope":
            "每个文件必须位于本任务的有界 write_scope 内；路径与 outputs 同为工作区相对路径，不能省略目录前缀。"
            "根目录文件可用精确文件 write_scope，不能用 '.'；仅引用前驱 API 时使用 inputs/depends_on，勿在本任务重复声明或扩大写权限",
        "api_contracts classes and methods must be public Python identifiers; do not list __init__":
            "类名及方法名必须是公开 Python 标识符，不以下划线开头，不使用关键字或列出 __init__",
        "api_contracts method names must be unique": "同一类的方法名不能重复",
    }
    hints = []
    for item in error.errors(include_input=False, include_url=False):
        path = prefix
        for part in item["loc"]:
            if isinstance(part, int):
                path += f"[{part}]"
            elif part in allowed:
                path += "." + part
            else:
                path += ".<extra_field>"  # Never echo model-chosen keys.
                break
        code, context = item["type"], item.get("ctx", {})
        api_message = api_errors.get(str(context.get("error"))) if code == "value_error" else None
        if api_message is not None:
            path += ".api_contracts"
            message = api_message
        elif code in {"string_too_long", "too_long"}:
            maximum = context.get("max_length")
            message = f"长度必须 ≤ {maximum}" if isinstance(maximum, int) else "超过 schema 长度上限"
        elif code in {"string_too_short", "too_short"}:
            minimum = context.get("min_length")
            message = f"长度必须 ≥ {minimum}" if isinstance(minimum, int) else "低于 schema 长度下限"
        elif code in {"greater_than", "greater_than_equal", "less_than", "less_than_equal"}:
            key = {"greater_than": "gt", "greater_than_equal": "ge", "less_than": "lt", "less_than_equal": "le"}[code]
            bound = context.get(key)
            symbol = {"gt": ">", "ge": "≥", "lt": "<", "le": "≤"}[key]
            message = f"数值必须 {symbol} {bound}" if isinstance(bound, (int, float)) else "数值超出 schema 允许范围"
        else:
            message = messages.get(code, "不符合该字段 schema 的类型或约束")
        hints.append(f"{path}: {message}")
    return hints


def _proposal_schema_error(hints):
    unique = list(dict.fromkeys(hints))
    extra = f"；另有 {len(unique) - 8} 项字段约束错误。" if len(unique) > 8 else ""
    return OutputSchemaError("invalid plan proposal schema",
                             hint="请定点修正字段并保留其他有效任务和检查：" + "；".join(unique[:8]) + extra)


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
        task["properties"]["api_contracts"]["description"] = (
            "APIs authored by this task only. Keys are exact workspace-relative .py paths, "
            "in the same coordinates as outputs and inside this task's bounded write_scope. "
            "Keep directory prefixes. Ancestor APIs are supplied via inputs/depends_on, not redeclared here."
        )
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
                "为每条需求及设计元素建立可验证任务；依据 coverage_obligations 选择任务类型，任务数和依赖由实际内容决定。",
                "遵守范围外限制、明确公开接口、执行者与复核者分离。对同一产物的测试由一个测试任务覆盖，"
                "将相关场景写入同一检查组，确保每条需求都能被测试。",
                "每个任务的 sources 必须同时包含至少一个 requirement_ids 原始 ID 和至少一个 design_element_ids 原始 ID。"
                "选择任务实际实现或验证的来源，逐项复制原始 ID，保留大小写和前缀。",
                "checks 写清需求指定的成功、失败、边界、状态转换、幂等和并发条件；保留数值范围、校验顺序及返回结构，不得缩减既有条件。",
                "用少量按业务场景组织的检查覆盖同一任务的相关断言，避免同一个场景占多个检查项。",
                "定义 Python 公开类的任务应声明 api_contracts：{精确相对.py路径:{公开类名:[承诺的公开方法名]}}。异常类用空数组；构造通过类调用，不列 __init__。名称为正式接口契约，下游不得猜测别名。仅声明本任务 write_scope 内的文件。",
                "api_contracts 仅声明本任务实际编写的 API；前驱 API 通过 inputs/depends_on 获得，不在只读测试或其他消费者任务中重复声明。",
                "api_contracts 的键、outputs、write_scope 都以工作区根为同一坐标，不能把 API 键当作 write_scope 目录内的短文件名。"
                "例如 outputs=['modules/Store.py']、write_scope=['modules/Store.py']，API 键须为 'modules/Store.py'，不能为 'Store.py'。"
                "根目录 interfaces.py 可直接用 write_scope=['interfaces.py']；不要使用 '.' 或扩大写范围。"
                "若确需移动文件，同步 API 键、outputs 和全部下游 inputs；优先保留既有路径并使用精确文件范围。",
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
            "coverage_obligations": self.coverage_obligations(req, ds),
            "out_of_scope": list(req.content["out_of_scope"]),
            "output_schema": self.draft_schema(),
            "constraints": [
                "executor != reviewer",
                "every requirement covered by a test task",
                "no accepted state or execution",
            ],
        }

    @staticmethod
    def coverage_obligations(req: Requirement, ds: Design, plan: Plan | None = None) -> dict[str, JsonValue]:
        """Describe the same task-type obligations used by program validation."""
        tasks = plan.tasks if plan is not None else []

        def coverage(source_id, allowed):
            return [task.task_id for task in tasks if task.type in allowed and source_id in task.sources]

        return {
            "requirements": [
                {"source_id": item["id"], "allowed_task_types": list(requirement_coverage_task_types(item)),
                 "required_delivery_kinds": list(delivery_obligations(item["text"])),
                 "covering_tasks": [task.task_id for task in requirement_covering_tasks(plan, item)] if plan else [],
                 "direct_covering_tasks": coverage(item["id"], requirement_coverage_task_types(item)),
                 "test_tasks": coverage(item["id"], ("test",))}
                for item in req.content["requirements"]
            ],
            "design_elements": [
                {"source_id": element["id"], "requirement_sources": element["sources"],
                 "allowed_task_types": list(design_coverage_task_types(element)),
                 "required_delivery_kinds": list(delivery_obligations(element["description"])),
                 "covering_tasks": [task.task_id for task in design_covering_tasks(plan, req, element)] if plan else [],
                 "direct_covering_tasks": coverage(element["id"], design_coverage_task_types(element))}
                for element in ds.content["elements"]
            ],
            "rules": [
                "allowed_task_types comes from the delivery explicitly requested by each source, not its ID prefix",
                "every requirement additionally needs explicit type=test coverage",
                "when using delivery tasks for coverage, satisfy every required_delivery_kind; test plus documentation requires both kinds",
                "a design element can also be covered through all of its valid requirement sources",
                "coverage references must match the actual task goal and checks; do not add unrelated sources",
                "test, integrate and doc tasks do not count as implementation of functional behavior",
            ],
        }

    def build_repair_context(self, plan: Plan, req: Requirement, ds: Design, findings: list[str], *,
                             review_context: dict[str, JsonValue] | None = None) -> dict[str, JsonValue]:
        """Keep the full repair backlog and prior obligations together for one revision."""
        context = deepcopy(review_context or {})
        active = [finding for finding in context.get("active_findings", [])
                  if isinstance(finding, dict) and finding.get("status", "open") == "open"
                  and finding.get("severity") in {"blocker", "important"}]
        resolved = [finding for finding in context.get("resolved_findings", [])
                    if isinstance(finding, dict) and finding.get("status") == "closed"]
        messages = list(dict.fromkeys([*findings, *(finding["message"] for finding in active
                                                  if isinstance(finding.get("message"), str))]))
        return {
            "previous_plan": plan.ref().model_dump(mode="json"),
            "active_findings": active,
            "review_findings": messages,
            "program_validation": validate_plan_semantics(plan, req, ds).model_dump(mode="json"),
            "coverage_obligations": self.coverage_obligations(req, ds, plan),
            "candidate_tasks": context.get("candidate_tasks", []),
            "candidate_profile_issues": context.get("candidate_profile_issues", []),
            "nonregression_checklist": {
                "existing_task_ids": [task.task_id for task in plan.tasks],
                "resolved_findings": resolved,
                "preserve": [
                    "Preserve existing task IDs, goals, checks, interfaces and dependency contracts unless a finding requires changing them",
                    "Retain all source-backed successful, failure, boundary, state, idempotency and concurrency cases already in previous_tasks",
                    "Preserve fixes cited in resolved_findings; do not reopen them through a task split or rename",
                    "Keep deliverables required and reachable through dependencies; do not replace executable work with a coverage label",
                    "Recheck every requirement and design obligation plus all program issues after the complete repair",
                ],
            },
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
            raise OutputSchemaError("plan output requires a non-empty tasks array; no fixture fallback",
                                    hint="tasks: 必须是非空数组(list)，每项为任务对象。")
        try:
            for key, expected in (("requirement", req.ref()), ("design", ds.ref())):
                if key in output:
                    try:
                        supplied = Ref.model_validate(output[key])
                    except ValidationError as exc:
                        raise _proposal_schema_error(_proposal_schema_hints(exc, key)) from exc
                    if supplied != expected:
                        raise OutputSchemaError("plan output has a stale input reference",
                                                hint=f"{key}: 必须绑定当前提供的版本和指纹，不得自行修改输入引用。")
            tasks, problems, restricted_root = [], [], False
            for index, raw in enumerate(raw_tasks):
                if not isinstance(raw, dict):
                    problems.append(f"tasks[{index}]: 必须是任务对象(object)")
                    continue
                # Models propose semantic fields. Program code owns all new version/hash values.
                data = {
                    key: value
                    for key, value in raw.items()
                    if key not in {"schema_version", "version", "content_hash"}
                }
                restricted_root = _restrict_root_scope(data) or restricted_root
                try:
                    data = TaskProposal.model_validate(data).model_dump(mode="json", exclude={"schema_version"})
                    if data.get("api_contracts") is None:
                        data.pop("api_contracts", None)  # Keep legacy task fingerprints byte-for-byte compatible.
                    tasks.append(Task(**data, version=1, content_hash=fingerprint(data)))
                except ValidationError as exc:
                    problems.extend(_proposal_schema_hints(exc, f"tasks[{index}]"))
            if problems:
                raise _proposal_schema_error(problems)
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
                ] + ([Provenance(
                    kind="material", source_id=_EXACT_OUTPUT_SCOPE_SOURCE,
                    locator="Program restriction: root write_scope narrowed to exact declared Python outputs before hashing; other task fields retained.",
                )] if restricted_root else []),
            )
        except OutputSchemaError:
            raise
        except ValidationError as exc:
            raise _proposal_schema_error(_proposal_schema_hints(exc, "plan")) from exc
        except (ValueError, TypeError) as exc:
            raise OutputSchemaError("invalid plan proposal",
                                    hint="tasks: 不符合任务结构、唯一编号或独立复核约束；请按提供的 schema 定点修正。") from exc

    def _plan_via_runner(self, req: Requirement, ds: Design, *, previous: Plan | None = None,
                         findings: list[str] | None = None,
                         repair_context: dict[str, JsonValue] | None = None) -> Plan:
        manifest = self.build_input_manifest(req, ds)
        refs = [req.ref(), ds.ref()]
        if previous is not None:
            refs.append(previous.ref())
            manifest["previous_tasks"] = [task.semantic_content() for task in previous.tasks]
            context = self.build_repair_context(previous, req, ds, findings or [], review_context=repair_context)
            manifest["repair_context"] = context
            manifest["review_findings"] = context["review_findings"]
            manifest["coverage_obligations"] = context["coverage_obligations"]
            manifest["prompt"] += (
                "\n这是一次完整修订：在同一轮处理 review_findings、repair_context.active_findings 中全部未关闭的阻断项及重要问题，"
                "同时修复 program_validation.issues 指出的确切任务/来源缺口；不要每轮只修第一项。"
                "对照 coverage_obligations 核对任务类型与实际目标/检查是否匹配，不得给无关实现任务追加测试或文档来源来凑覆盖。"
                "逐项保留 nonregression_checklist 和 previous_tasks 中未受影响的已满足条件及已关闭问题的修复证据。"
                "若 repair_context.candidate_tasks 非空，它是尚未复审的最新修改草稿；以该草稿为起点修复剩余 "
                "candidate_profile_issues，同时满足完整审查清单，保留已经改正确的部分，不要退回旧版重新生成。"
                "修正受影响的目标、检查、来源和依赖后，再整体检查所有任务；拆分任务时保留已有 ID 并维持前驱输入供应。"
                "仅返回修订后的全部 tasks，不返回局部补丁、审查通过声明或自拟的关闭结论；独立复核由 Momus 完成。"
            )
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

        def validate_proposal(value):
            proposed = self._materialize(value, req, ds, mode)
            if previous is not None:
                if {
                    task.task_id: task.content_hash for task in proposed.tasks
                } == {task.task_id: task.content_hash for task in previous.tasks}:
                    raise OutputSchemaError(
                        "plan revision made no semantic change",
                        hint="修订任务与上一版内容完全相同；请在这一轮修复全部未关闭问题，修改对应任务目标、检查或依赖后返回完整 tasks。",
                    )
                report = validate_plan_semantics(proposed, req, ds)
                blockers = [issue for issue in report.issues if issue.severity == "blocker"]
                if blockers:
                    gaps = [{"code": issue.code, "task_id": (issue.task_id or "")[:80],
                             "source_id": (issue.source_id or "")[:80]} for issue in blockers[:12]]
                    raise OutputSchemaError(
                        "revised plan still has deterministic blockers",
                        hint=(
                            f"程序校验发现修订后仍有 {len(blockers)} 项阻断，以下最多列出 12 项的代码及任务/来源 ID："
                            + json.dumps(gaps, ensure_ascii=False)
                            + "。请同时修复全部程序缺口与审查问题，返回完整 tasks；"
                              "按来源的实际任务类型和内容修复覆盖，不得向无关任务添加 sources 凑数。"
                        ),
                    )
            return proposed

        role_result = self._runner.run(
            run_input, output_validator=validate_proposal
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
        safely (cycles, out-of-scope work) survive into the revision
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

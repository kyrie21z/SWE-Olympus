"""Programmatic semantic validation for plans.

Covers DAG shape, source references, REQ/DS coverage, test coverage,
out-of-scope terms, input supply (including acyclic plans whose inputs are not
actually produced upstream) and write-scope normalization. The validator emits
deterministic blocker issues; it never approves a plan, never marks a task
accepted and never triggers execution.
"""

from __future__ import annotations

import re
from typing import Literal

from olympus.contracts.models import (
    Contract,
    Design,
    Id,
    NonNegative,
    Plan,
    Ref,
    Requirement,
    Task,
)

IssueCode = Literal[
    "CYCLE_DETECTED",
    "UNKNOWN_DEPENDENCY",
    "MISSING_SOURCE",
    "UNCOVERED_REQUIREMENT",
    "UNCOVERED_DESIGN",
    "MISSING_TEST_COVERAGE",
    "OUT_OF_SCOPE_VIOLATION",
    "MISSING_INPUT_SUPPLY",
    "INVALID_WRITE_SCOPE",
    "SELF_REVIEW",
    "STALE_INPUT_REFERENCE",
]

IMPLEMENTATION_TYPES = ("interface", "implement")

# Only the primary deliverable names identify these exceptions. Acceptance text
# describes how business behaviour is tested, so it must never classify coverage.
_DELIVERY_PREFIX = re.compile(
    r"^(?:(?:提供|交付|编写|准备|生成|建立|维护|提交|包含)\s*|"
    r"(?:provide|deliver|write|create|prepare|generate|maintain|include)\s+)?"
    r"(?:(?:可重复执行的|可重复运行的|可复现的|完整的|全面的|独立的|一套|一组|中文|英文)|"
    r"(?:repeatable|reproducible|independent|complete|chinese|english)\s+)*", re.IGNORECASE,
)
_TEST_DELIVERABLE = re.compile(
    r"自动化测试|自动化验收|测试套件|测试脚本|测试与验收|"
    r"(?:unittest|pytest)\s+(?:suite|tests?)\b|automated\s+tests?\b|"
    r"test(?:ing)?\s+(?:suites?|scripts?|cases?)\b", re.IGNORECASE,
)
_DOC_DELIVERABLE = re.compile(
    r"(?:运行|使用|安装|部署)(?:说明|指南|文档)|技术文档|交付文档|README\b|"
    r"(?:run|running|usage|installation|deployment)\s+(?:instructions?|guides?)\b|"
    r"documentation\b|user\s+manual\b", re.IGNORECASE,
)
_IMPLEMENTATION_CLAUSE = re.compile(
    r"(?:并|同时|以及|且|[;；。])\s*(?:实现|处理|支持|新增|创建|返回|保证)|"
    r"\b(?:and|while)\s+(?:implement|process|support|create|return|enforce)\b", re.IGNORECASE,
)
_DELIVERY_TASK_TYPES = {"test": ("test", "integrate"), "doc": ("doc",)}

_TERM_SPLIT = re.compile(r"[/、,，;；\s]+")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class PlanIssue(Contract):
    issue_id: Id
    code: IssueCode
    severity: Literal["blocker", "important"] = "blocker"
    task_id: str | None = None
    source_id: str | None = None
    message: str


class PlanValidationReport(Contract):
    plan_ref: Ref
    req_ref: Ref
    ds_ref: Ref
    valid: bool
    edge_count: NonNegative
    issues: list[PlanIssue]


def requirement_ids(req: Requirement) -> list[str]:
    return [item["id"] for item in req.content["requirements"]]


def design_element_ids(ds: Design) -> list[str]:
    return [element["id"] for element in ds.content["elements"]]


def forbidden_terms(req: Requirement) -> list[str]:
    """Declared out-of-scope tokens plus their compound parts, longest first."""
    terms: set[str] = set()
    for token in req.content["out_of_scope"]:
        token = token.strip()
        if not token:
            continue
        terms.add(token)
        terms.update(part for part in _TERM_SPLIT.split(token) if part)
    return sorted(terms, key=lambda term: (-len(term), term))


def dependency_graph(plan: Plan) -> dict[str, list[str]]:
    """Task ID to known dependency IDs; unknown dependencies belong to their own issue."""
    known = {task.task_id for task in plan.tasks}
    return {task.task_id: [dep for dep in task.depends_on if dep in known] for task in plan.tasks}


def unknown_dependencies(plan: Plan) -> list[tuple[str, str]]:
    known = {task.task_id for task in plan.tasks}
    return [(task.task_id, dep) for task in plan.tasks for dep in task.depends_on if dep not in known]


def ancestor_map(plan: Plan) -> dict[str, list[str]]:
    """Transitive dependencies for each task, excluding the task itself."""
    graph = dependency_graph(plan)
    result: dict[str, list[str]] = {}
    for task_id in graph:
        seen: set[str] = set()
        frontier = list(graph[task_id])
        while frontier:
            node = frontier.pop()
            if node in seen:
                continue
            seen.add(node)
            frontier.extend(graph[node])
        result[task_id] = sorted(seen)
    return result


def _canonical_cycle(cycle: list[str]) -> tuple[str, ...]:
    body = cycle[:-1]
    if not body:
        return tuple(cycle)
    start = body.index(min(body))
    rotated = body[start:] + body[:start]
    return (*rotated, rotated[0])


def find_dependency_cycles(plan: Plan) -> list[list[str]]:
    """Return each dependency cycle once, as a closed path such as T02 -> T05 -> T02."""
    graph = dependency_graph(plan)
    state: dict[str, int] = {}
    stack: list[str] = []
    found: dict[tuple[str, ...], list[str]] = {}

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for dep in graph[node]:
            if state.get(dep, 0) == 0:
                visit(dep)
            elif state.get(dep) == 1:
                cycle = stack[stack.index(dep) :] + [dep]
                canonical = _canonical_cycle(cycle)
                found.setdefault(canonical, list(canonical))
        stack.pop()
        state[node] = 2

    for node in sorted(graph):
        if state.get(node, 0) == 0:
            visit(node)
    return [found[key] for key in sorted(found)]


def write_scope_problem(scope: str) -> str | None:
    """Return a rejection reason for absolute or traversal write scopes."""
    normalized = scope.replace("\\", "/")
    if not normalized:
        return "empty_path"
    if normalized.startswith("/") or normalized.startswith("~") or _DRIVE_PREFIX.match(normalized):
        return "absolute_path"
    if ".." in normalized.split("/"):
        return "parent_traversal"
    return None


def coverage_obligations(text: str) -> tuple[str, ...]:
    """Recognize explicit test/document deliverables; default to implementation.

    This is a conservative planning rule, not semantic approval. A statement
    about a business service remains implementation work even if its later
    clauses mention tests or documentation. Both named deliverables need owners.
    """
    primary = _DELIVERY_PREFIX.sub("", text.strip(), count=1)
    if (_IMPLEMENTATION_CLAUSE.search(primary) or
            not (_TEST_DELIVERABLE.match(primary) or _DOC_DELIVERABLE.match(primary))):
        return ("implementation",)
    return tuple(kind for kind, pattern in (("test", _TEST_DELIVERABLE), ("doc", _DOC_DELIVERABLE))
                 if pattern.search(primary))


def _coverage_task_types(text: str) -> tuple[str, ...]:
    extra = [kind for obligation in coverage_obligations(text)
             for kind in _DELIVERY_TASK_TYPES.get(obligation, ())]
    return (*IMPLEMENTATION_TYPES, *extra)


def requirement_coverage_task_types(item: dict) -> tuple[str, ...]:
    return _coverage_task_types(item["text"])


def design_coverage_task_types(element: dict) -> tuple[str, ...]:
    return _coverage_task_types(element["description"])


def _direct_covering_tasks(plan: Plan, source_id: str, text: str) -> list[Task]:
    linked = [task for task in plan.tasks if source_id in task.sources]
    implementation = [task for task in linked if task.type in IMPLEMENTATION_TYPES]
    if implementation:
        return implementation
    obligations = coverage_obligations(text)
    if obligations == ("implementation",):
        return []
    if not all(any(task.type in _DELIVERY_TASK_TYPES[kind] for task in linked) for kind in obligations):
        return []
    allowed = _coverage_task_types(text)
    return [task for task in linked if task.type in allowed]


def requirement_covering_tasks(plan: Plan, item: dict) -> list[Task]:
    return _direct_covering_tasks(plan, item["id"], item["text"])


def uncovered_requirements(plan: Plan, req: Requirement) -> list[str]:
    return [item["id"] for item in req.content["requirements"] if not requirement_covering_tasks(plan, item)]


def design_covering_tasks(plan: Plan, req: Requirement, element: dict) -> list[Task]:
    direct = _direct_covering_tasks(plan, element["id"], element["description"])
    if direct:
        return direct
    items = {item["id"]: item for item in req.content["requirements"]}
    sources = [items[source] for source in element["sources"] if source in items]
    if coverage_obligations(element["description"]) == ("implementation",):
        # A business design element retains its implementation obligation even
        # when someone links it only to a document/test delivery requirement.
        covering = [_direct_covering_tasks(plan, item["id"], "") for item in sources]
    else:
        covering = [requirement_covering_tasks(plan, item) for item in sources]
    if not covering or not all(covering):
        return []
    selected = {task.task_id for tasks in covering for task in tasks}
    return [task for task in plan.tasks if task.task_id in selected]


def uncovered_design_elements(plan: Plan, req: Requirement, ds: Design) -> list[str]:
    """Use direct deliverable owners or complete coverage of an element's sources."""
    return [element["id"] for element in ds.content["elements"] if not design_covering_tasks(plan, req, element)]


def scan_out_of_scope(req: Requirement, tasks: list[Task]) -> list[dict[str, str]]:
    """Find out-of-scope tokens in task goal, sources, outputs, write scope and checks."""
    terms = forbidden_terms(req)
    hits: list[dict[str, str]] = []
    for task in tasks:
        fields = (
            ("goal", [task.goal]),
            ("sources", task.sources),
            ("outputs", task.outputs),
            ("write_scope", task.write_scope),
            ("checks", task.checks),
        )
        for field, values in fields:
            text = " ".join(values)
            match = next((term for term in terms if term in text), None)
            if match is not None:
                hits.append({"task_id": task.task_id, "term": match, "field": field})
    hits.sort(key=lambda hit: (hit["task_id"], hit["field"], hit["term"]))
    return hits


def _draft(code: IssueCode, message: str, task_id: str | None = None, source_id: str | None = None):
    return {"code": code, "message": message, "task_id": task_id, "source_id": source_id}


def _issue_sort_key(draft: dict) -> tuple[str, str, str, str]:
    return (
        draft["code"],
        draft["task_id"] or "",
        draft["source_id"] or "",
        draft["message"],
    )


def validate_plan_semantics(plan: Plan, req: Requirement, ds: Design) -> PlanValidationReport:
    drafts: list[dict] = []
    for name, actual, expected in (
        ("plan.requirement", plan.requirement, req.ref()),
        ("plan.design", plan.design, ds.ref()),
        ("design.requirement", ds.requirement, req.ref()),
    ):
        if actual != expected:
            drafts.append(_draft("STALE_INPUT_REFERENCE", f"{name} does not bind the supplied version/hash"))
    req_ids = requirement_ids(req)
    ds_ids = design_element_ids(ds)
    known_sources = set(req_ids) | set(ds_ids)

    # 1. Dependency shape: unknown dependencies and cycles.
    for task_id, dep in unknown_dependencies(plan):
        drafts.append(
            _draft(
                "UNKNOWN_DEPENDENCY",
                f"任务 {task_id} 依赖未定义的任务 {dep}",
                task_id=task_id,
                source_id=dep,
            )
        )
    for cycle in find_dependency_cycles(plan):
        drafts.append(
            _draft(
                "CYCLE_DETECTED",
                "任务依赖成环：" + " -> ".join(cycle),
                task_id=cycle[0],
            )
        )

    # 2. Source references: no dangling IDs and at least one REQ plus one DS source.
    for task in plan.tasks:
        for source in task.sources:
            if source not in known_sources:
                drafts.append(
                    _draft(
                        "MISSING_SOURCE",
                        f"任务 {task.task_id} 引用了不存在于需求或设计中的来源 {source}",
                        task_id=task.task_id,
                        source_id=source,
                    )
                )
        if not any(source in req_ids for source in task.sources):
            drafts.append(
                _draft(
                    "MISSING_SOURCE",
                    f"任务 {task.task_id} 未引用任何有效需求（FR-*）来源",
                    task_id=task.task_id,
                )
            )
        if not any(source in ds_ids for source in task.sources):
            drafts.append(
                _draft(
                    "MISSING_SOURCE",
                    f"任务 {task.task_id} 未引用任何有效设计元素（D-*）来源",
                    task_id=task.task_id,
                )
            )

    # 3. Business implementation, with explicit test/document delivery exceptions.
    missing_req = set(uncovered_requirements(plan, req))
    for item in req.content["requirements"]:
        req_id = item["id"]
        if req_id in missing_req:
            obligations = coverage_obligations(item["text"])
            detail = "实现或接口任务" if obligations == ("implementation",) else "符合其测试、集成或文档交付类型的任务"
            drafts.append(
                _draft(
                    "UNCOVERED_REQUIREMENT",
                    f"需求 {req_id} 没有被任何{detail}覆盖",
                    source_id=req_id,
                )
            )
    for ds_id in uncovered_design_elements(plan, req, ds):
        drafts.append(
            _draft(
                "UNCOVERED_DESIGN",
                f"设计元素 {ds_id} 没有被适合其交付类型的任务覆盖，且其来源需求尚未全部覆盖",
                source_id=ds_id,
            )
        )

    # 4. Test coverage: every requirement needs a task of type "test".
    tested = {source for task in plan.tasks if task.type == "test" for source in task.sources}
    for req_id in req_ids:
        if req_id not in tested:
            drafts.append(
                _draft(
                    "MISSING_TEST_COVERAGE",
                    f"需求 {req_id} 没有测试任务覆盖",
                    source_id=req_id,
                )
            )

    # 5. Out-of-scope and forbidden items.
    for hit in scan_out_of_scope(req, plan.tasks):
        drafts.append(
            _draft(
                "OUT_OF_SCOPE_VIOLATION",
                f"任务 {hit['task_id']} 的 {hit['field']} 出现范围外内容 {hit['term']}",
                task_id=hit["task_id"],
                source_id=hit["term"],
            )
        )

    # 6. Input supply, direct or transitive, even for acyclic graphs.
    producers: dict[str, str] = {}
    for task in plan.tasks:
        for output in task.outputs:
            producers.setdefault(output, task.task_id)
    ancestors = ancestor_map(plan)
    for task in plan.tasks:
        for item in task.inputs:
            if not item.startswith("artifact:"):
                continue
            producer = producers.get(item)
            if producer is None:
                drafts.append(
                    _draft(
                        "MISSING_INPUT_SUPPLY",
                        f"任务 {task.task_id} 的输入 {item} 没有任何任务产出",
                        task_id=task.task_id,
                        source_id=item,
                    )
                )
                continue
            if producer not in ancestors[task.task_id]:
                drafts.append(
                    _draft(
                        "MISSING_INPUT_SUPPLY",
                        f"任务 {task.task_id} 的输入 {item} 由 {producer} 产出，但 {producer} 不是其传递前驱",
                        task_id=task.task_id,
                        source_id=producer,
                    )
                )

    # 7. Write scope normalization.
    for task in plan.tasks:
        for scope in task.write_scope:
            problem = write_scope_problem(scope)
            if problem is not None:
                drafts.append(
                    _draft(
                        "INVALID_WRITE_SCOPE",
                        f"任务 {task.task_id} 的写入范围 {scope} 非法（{problem}）",
                        task_id=task.task_id,
                        source_id=scope,
                    )
                )

    # Defensive: the Task contract already rejects self-review at construction.
    for task in plan.tasks:
        if task.executor == task.reviewer:
            drafts.append(
                _draft(
                    "SELF_REVIEW",
                    f"任务 {task.task_id} 的执行者与复核者相同",
                    task_id=task.task_id,
                )
            )

    drafts.sort(key=_issue_sort_key)
    issues = [
        PlanIssue(
            issue_id=f"ISSUE-{index:02d}",
            code=draft["code"],
            task_id=draft["task_id"],
            source_id=draft["source_id"],
            message=draft["message"],
        )
        for index, draft in enumerate(drafts, 1)
    ]
    return PlanValidationReport(
        plan_ref=plan.ref(),
        req_ref=req.ref(),
        ds_ref=ds.ref(),
        valid=not any(issue.severity == "blocker" for issue in issues),
        edge_count=sum(len(task.depends_on) for task in plan.tasks),
        issues=issues,
    )

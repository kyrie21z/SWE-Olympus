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
]

IMPLEMENTATION_TYPES = ("interface", "implement")
WAITLIST_SERVICE_TASK = "T03"
WAITLIST_STATE_REQUIREMENT = "FR-005"

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
    return [
        (task.task_id, dep)
        for task in plan.tasks
        for dep in task.depends_on
        if dep not in known
    ]


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


def uncovered_design_elements(plan: Plan, req: Requirement, ds: Design) -> list[str]:
    """Design elements with no implementation coverage.

    An element is covered when an implementation/interface task names it
    directly, or when every requirement it sources is covered by such a task.
    """
    implementation = [task for task in plan.tasks if task.type in IMPLEMENTATION_TYPES]
    referenced = {source for task in implementation for source in task.sources}
    req_ids = set(requirement_ids(req))
    covered_req = {source for source in referenced if source in req_ids}
    missing: list[str] = []
    for element in ds.content["elements"]:
        if element["id"] in referenced:
            continue
        sources = [source for source in element["sources"] if source in req_ids]
        if not sources or not all(source in covered_req for source in sources):
            missing.append(element["id"])
    return missing


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


def _waitlist_service(plan: Plan) -> str | None:
    """The task that owns waitlist state; standard plans name it T03."""
    for task in plan.tasks:
        if task.task_id == WAITLIST_SERVICE_TASK and task.type in IMPLEMENTATION_TYPES:
            return WAITLIST_SERVICE_TASK
    candidates = [
        task.task_id
        for task in plan.tasks
        if task.type == "implement"
        and "FR-003" in task.sources
        and "候补" in task.goal
    ]
    return min(candidates) if candidates else None


def _consumes_waitlist_state(task: Task) -> bool:
    """Implementation work that reads waitlist state must receive the waitlist service.

    Interface and doc cards may name every FR without consuming runtime state, so
    only implementation tasks (and the standard status page marker) are bound.
    """
    if task.type != "implement":
        return False
    return (
        task.task_id == "T05"
        or WAITLIST_STATE_REQUIREMENT in task.sources
        or "状态查询" in task.goal
    )


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

    # 3. Requirement and design coverage by implementation or interface tasks.
    implementation = [task for task in plan.tasks if task.type in IMPLEMENTATION_TYPES]
    covered_req = {source for task in implementation for source in task.sources}
    for req_id in req_ids:
        if req_id not in covered_req:
            drafts.append(
                _draft(
                    "UNCOVERED_REQUIREMENT",
                    f"需求 {req_id} 没有被任何实现或接口任务覆盖",
                    source_id=req_id,
                )
            )
    for ds_id in uncovered_design_elements(plan, req, ds):
        drafts.append(
            _draft(
                "UNCOVERED_DESIGN",
                f"设计元素 {ds_id} 没有被任何实现或接口任务覆盖",
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
    supplied: set[tuple[str, str]] = set()
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
                supplied.add((task.task_id, producer))

    waitlist = _waitlist_service(plan)
    if waitlist is not None:
        artifact = f"artifact:{waitlist}"
        for task in plan.tasks:
            if task.task_id == waitlist or not _consumes_waitlist_state(task):
                continue
            if artifact in task.inputs and waitlist in task.depends_on:
                continue
            if (task.task_id, waitlist) in supplied:
                continue
            drafts.append(
                _draft(
                    "MISSING_INPUT_SUPPLY",
                    f"任务 {task.task_id} 依赖候补状态查询，但缺少 {artifact} 输入或 {waitlist} 依赖",
                    task_id=task.task_id,
                    source_id=waitlist,
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

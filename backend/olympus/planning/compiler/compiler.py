"""Deterministic static plan compiler.

Turns a plan into a manifest with topological layers, ancestor/descendant
indexes, a static critical-path heuristic and resource/write-scope conflicts.
Compilation is pure: it never marks a task accepted and never triggers
execution.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from typing import Literal

from pydantic import model_validator

from olympus.contracts.models import (
    Contract,
    Design,
    Hash,
    Id,
    NonNegative,
    Plan,
    Positive,
    Ref,
    Requirement,
    fingerprint,
)
from olympus.planning.validator import (
    ancestor_map,
    dependency_graph,
    find_dependency_cycles,
    unknown_dependencies,
    validate_plan_semantics,
)

HEURISTIC_KIND = "remaining_layer_depth_and_fanout"
HEURISTIC_DISCLAIMER = "静态层级与剩余关键路径启发式，无实测工时依据，不作为工期承诺"
COMPILER_VERSION = "1.0"


class PlanCompilationError(ValueError):
    """Raised when a plan cannot be compiled deterministically."""


class StaleManifestError(ValueError):
    """Raised when a compiled manifest no longer binds the plan it came from."""


class CriticalPathIndex(Contract):
    heuristic_kind: Literal["remaining_layer_depth_and_fanout"] = HEURISTIC_KIND
    disclaimer: Id = HEURISTIC_DISCLAIMER
    remaining_depth: dict[Id, Positive]
    critical_chain: list[Id]
    priority_rank: dict[Id, Positive]


class StaticConflictPair(Contract):
    task_a: Id
    task_b: Id
    conflict_kinds: list[Literal["write_scope", "resource", "executor"]]
    details: list[Id]


class StaticResourceConflictIndex(Contract):
    write_scope_groups: dict[Id, list[Id]]
    resource_groups: dict[Id, list[Id]]
    executor_groups: dict[Id, list[Id]]
    concurrent_conflicts: list[StaticConflictPair]


class CompiledManifest(Contract):
    manifest_id: Id
    compiler_version: Literal["1.0"] = COMPILER_VERSION
    plan_ref: Ref
    plan_hash: Hash
    edge_count: NonNegative
    topological_order: list[Id]
    layers: list[list[Id]]
    predecessors: dict[Id, list[Id]]
    ancestors: dict[Id, list[Id]]
    successors: dict[Id, list[Id]]
    descendants: dict[Id, list[Id]]
    critical_path: CriticalPathIndex
    resource_conflicts: StaticResourceConflictIndex
    task_hashes: dict[Id, Hash]
    manifest_hash: Hash

    @model_validator(mode="after")
    def self_consistent(self):
        if self.manifest_hash != manifest_digest(self):
            raise ValueError("manifest_hash does not match canonical semantic fields")
        return self


def manifest_digest(manifest: CompiledManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"schema_version", "manifest_hash"})
    return fingerprint(payload)


def _ordered(ids, priority: dict[str, int]) -> list[str]:
    return sorted(ids, key=lambda task_id: (-priority[task_id], task_id))


def _topological_order(
    graph: dict[str, list[str]], successors: dict[str, list[str]], priority: dict[str, int]
) -> list[str]:
    indegree = {task_id: len(deps) for task_id, deps in graph.items()}
    heap = [(-priority[task_id], task_id) for task_id, degree in indegree.items() if degree == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        _, task_id = heapq.heappop(heap)
        order.append(task_id)
        for successor in successors[task_id]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heapq.heappush(heap, (-priority[successor], successor))
    if len(order) != len(graph):
        raise PlanCompilationError("plan contains a dependency cycle")
    return order


def _descendant_map(successors: dict[str, list[str]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for task_id in successors:
        seen: set[str] = set()
        frontier = list(successors[task_id])
        while frontier:
            node = frontier.pop()
            if node in seen:
                continue
            seen.add(node)
            frontier.extend(successors[node])
        result[task_id] = sorted(seen)
    return result


def _paths_overlap(left: str, right: str) -> bool:
    left = left.replace("\\", "/").rstrip("/")
    right = right.replace("\\", "/").rstrip("/")
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


def _conflict_index(
    plan: Plan, ancestors: dict[str, list[str]]
) -> StaticResourceConflictIndex:
    write_scope_groups: dict[str, list[str]] = defaultdict(list)
    resource_groups: dict[str, list[str]] = defaultdict(list)
    executor_groups: dict[str, list[str]] = defaultdict(list)
    for task in plan.tasks:
        for scope in task.write_scope:
            write_scope_groups[scope].append(task.task_id)
        for resource in task.resources:
            resource_groups[resource].append(task.task_id)
        executor_groups[task.executor].append(task.task_id)

    conflicts: list[StaticConflictPair] = []
    for index, task_a in enumerate(plan.tasks):
        for task_b in plan.tasks[index + 1 :]:
            a, b = task_a.task_id, task_b.task_id
            if b in ancestors[a] or a in ancestors[b]:
                continue
            kinds: list[str] = []
            details: list[str] = []
            overlaps: list[str] = []
            for scope_a in task_a.write_scope:
                for scope_b in task_b.write_scope:
                    if _paths_overlap(scope_a, scope_b):
                        for scope in (scope_a, scope_b):
                            if scope not in overlaps:
                                overlaps.append(scope)
            if overlaps:
                kinds.append("write_scope")
                details.extend(overlaps)
            shared_resources = sorted(set(task_a.resources) & set(task_b.resources))
            if shared_resources:
                kinds.append("resource")
                details.extend(shared_resources)
            if task_a.executor == task_b.executor:
                kinds.append("executor")
                details.append(task_a.executor)
            if kinds:
                conflicts.append(
                    StaticConflictPair(
                        task_a=a, task_b=b, conflict_kinds=kinds, details=details
                    )
                )
    conflicts.sort(key=lambda pair: (pair.task_a, pair.task_b))
    return StaticResourceConflictIndex(
        write_scope_groups=dict(sorted(write_scope_groups.items())),
        resource_groups=dict(sorted(resource_groups.items())),
        executor_groups=dict(sorted(executor_groups.items())),
        concurrent_conflicts=conflicts,
    )


def _critical_path(
    order: list[str], successors: dict[str, list[str]], priority: dict[str, int]
) -> CriticalPathIndex:
    depth = {task_id: 1 for task_id in order}
    for task_id in reversed(order):
        for successor in successors[task_id]:
            depth[task_id] = max(depth[task_id], depth[successor] + 1)
    start = min(order, key=lambda task_id: (-depth[task_id], task_id))
    chain = [start]
    while successors[chain[-1]]:
        chain.append(min(successors[chain[-1]], key=lambda task_id: (-depth[task_id], task_id)))
    descendants = _descendant_map(successors)
    ranked = sorted(
        order,
        key=lambda task_id: (-depth[task_id], -len(descendants[task_id]), -priority[task_id], task_id),
    )
    return CriticalPathIndex(
        remaining_depth=depth,
        critical_chain=chain,
        priority_rank={task_id: rank for rank, task_id in enumerate(ranked, 1)},
    )


def _require_compilable(plan: Plan) -> None:
    unknown = unknown_dependencies(plan)
    if unknown:
        task_id, dep = unknown[0]
        raise PlanCompilationError(f"task {task_id} depends on unknown task {dep}")
    cycles = find_dependency_cycles(plan)
    if cycles:
        raise PlanCompilationError("dependency cycle: " + " -> ".join(cycles[0]))


def compile_plan(
    plan: Plan, req: Requirement | None = None, ds: Design | None = None
) -> CompiledManifest:
    """Compile a plan into a deterministic manifest without accepting any task."""
    _require_compilable(plan)
    if (req is None) != (ds is None):
        raise ValueError("req and ds must be provided together")
    if req is not None and ds is not None:
        report = validate_plan_semantics(plan, req, ds)
        if not report.valid:
            blockers = ", ".join(
                f"{issue.code}({issue.task_id or issue.source_id or 'plan'})"
                for issue in report.issues
                if issue.severity == "blocker"
            )
            raise PlanCompilationError(f"plan failed semantic validation: {blockers}")

    priority = {task.task_id: task.priority for task in plan.tasks}
    graph = dependency_graph(plan)
    successors: dict[str, list[str]] = {task_id: [] for task_id in graph}
    for task_id, deps in graph.items():
        for dep in deps:
            successors[dep].append(task_id)
    for task_id in successors:
        successors[task_id] = _ordered(successors[task_id], priority)

    order = _topological_order(graph, successors, priority)
    generation = {task_id: 0 for task_id in order}
    for task_id in order:
        for successor in successors[task_id]:
            generation[successor] = max(generation[successor], generation[task_id] + 1)
    layer_count = max(generation.values(), default=0) + 1
    layers = [
        _ordered([task_id for task_id in order if generation[task_id] == layer], priority)
        for layer in range(layer_count)
    ]

    ancestors_raw = ancestor_map(plan)
    ancestors = {task_id: _ordered(values, priority) for task_id, values in ancestors_raw.items()}
    descendants = {
        task_id: _ordered(values, priority)
        for task_id, values in _descendant_map(successors).items()
    }
    fields = {
        "manifest_id": f"MANIFEST-{plan.object_id[:120]}-V{plan.version}",
        "compiler_version": COMPILER_VERSION,
        "plan_ref": plan.ref(),
        "plan_hash": plan.content_hash,
        "edge_count": sum(len(task.depends_on) for task in plan.tasks),
        "topological_order": order,
        "layers": layers,
        "predecessors": {task_id: _ordered(deps, priority) for task_id, deps in graph.items()},
        "ancestors": ancestors,
        "successors": successors,
        "descendants": descendants,
        "critical_path": _critical_path(order, successors, priority),
        "resource_conflicts": _conflict_index(plan, ancestors_raw),
        "task_hashes": {task.task_id: task.content_hash for task in plan.tasks},
    }
    draft = CompiledManifest.model_construct(**fields, schema_version="1.0", manifest_hash="0" * 64)
    return CompiledManifest(**fields, manifest_hash=manifest_digest(draft))


def verify_manifest_binding(manifest: CompiledManifest, plan: Plan) -> bool:
    """Confirm a manifest still binds the exact plan version it was compiled from."""
    if manifest.plan_ref != plan.ref():
        raise StaleManifestError("manifest plan_ref does not match the current plan version")
    if manifest.plan_hash != plan.content_hash:
        raise StaleManifestError("manifest plan_hash does not match the current plan content")
    expected = {task.task_id: task.content_hash for task in plan.tasks}
    if manifest.task_hashes != expected:
        raise StaleManifestError("manifest task hashes do not match the current plan tasks")
    if manifest.manifest_hash != manifest_digest(manifest):
        raise StaleManifestError("manifest hash is not self-consistent")
    return True

"""Shared, program-owned capabilities of the trusted local execution adapter."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from olympus.contracts.models import Plan
from olympus.planning.validator import ancestor_map

IMPLEMENTATION_FILE_SUFFIXES = frozenset({".py", ".json", ".md", ".txt"})
IMPLEMENTATION_TASK_TYPES = frozenset({"interface", "implement"})
READ_ONLY_TASK_TYPES = frozenset({"test", "integrate", "doc"})

LOCAL_EXECUTION_CAPABILITIES = {
    "implementation": {
        "task_types": sorted(IMPLEMENTATION_TASK_TYPES), "executor": "Hephaestus", "reviewer": "Argus",
        "file_suffixes": sorted(IMPLEMENTATION_FILE_SUFFIXES),
        "imports": ["import math", "from threading import Lock", "from threading import RLock"],
        "unsupported": [".pyi stubs", "typing imports", "third-party imports", "authoring unittest scripts"],
        "artifacts": "Author required implementation and documentation files only inside bounded write_scope.",
    },
    "checking": {
        "task_types": sorted(READ_ONLY_TASK_TYPES), "executors": ["Momus", "Hermes"], "reviewer": "Argus",
        "behavior": "Run independently frozen Momus acceptance scripts against immutable declared ancestor inputs; no implementation model call.",
        "write_scope": "A bounded report directory, never a source/document filename.",
        "generated_report": "{write_scope[0]}/check-report.json",
        "outputs": "Use logical artifact IDs or the actual generated report path. A source/document filename needs an existing declared ancestor author; this task does not create that file.",
    },
    "acceptance": {
        "author": "Momus", "timing": "Frozen independently before implementation.",
        "imports": ["unittest", "math", "threading", "concurrent.futures", "declared executable input modules"],
        "source_facts": "Use trusted source_metadata for static source facts; do not import inspect/ast or use reflection.",
        "behavior_checks": "Call available public executable interfaces for actual behavior. A static interface contract cannot prove behavior only implemented by later tasks.",
    },
    "source_metadata": {
        "python": ["sha256", "kind", "import_roots", "public_symbols", "signatures", "initializers", "annotations", "docstrings"],
        "text": "For .md/.txt files, inspect trusted text metadata; do not open files.",
        "json": "For .json files, inspect trusted parsed data metadata; reports contain task, executor, checks and inputs.",
        "reports": "Read-only task reports are real JSON files at <report-directory>/check-report.json; use trusted report metadata rather than imaginary authored test modules.",
    },
    "max_tasks": 30,
    "paths": "Relative bounded paths only; no drives, traversal, hidden/control paths or unsupported file suffixes.",
}


def _normalized(value: str) -> str:
    return value.replace("\\", "/")


def _path_problem(value: str) -> str | None:
    normalized = _normalized(value)
    path = PurePosixPath(normalized)
    if (not normalized or normalized in {".", "/"} or normalized.startswith(("/", "~"))
            or ":" in normalized or ".." in path.parts):
        return "unbounded"
    if any(part.startswith(".") or part == "__pycache__" for part in path.parts):
        return "control"
    return None


def _file_claim(value: str) -> bool:
    # Output IDs such as artifact:T01 are logical names, not filesystem paths.
    return ":" not in value and bool(PurePosixPath(_normalized(value)).suffix)


def _label(value: str) -> str:
    # Model-supplied paths are bounded, quoted data rather than correction prose.
    return json.dumps(value[:120], ensure_ascii=False)


def execution_profile_issues(plan: Plan) -> list[dict[str, str]]:
    """Reject plans that require files/authoring the local adapter cannot supply.

    This checks execution capabilities only. Semantic coverage and independent
    review still decide whether tasks satisfy the requirement and design.
    """
    issues: list[dict[str, str]] = []
    ancestors = ancestor_map(plan)
    by_id = {task.task_id: task for task in plan.tasks}
    producers: dict[str, list] = {}
    for task in plan.tasks:
        for output in task.outputs:
            producers.setdefault(output, []).append(task)

    def add(code, task, message, hint):
        issues.append({"code": code, "task_id": task.task_id[:128],
                       "message": message[:400], "hint": hint[:600]})

    def available_authors(consumer):
        found, pending = set(), [consumer]
        while pending:
            current = pending.pop()
            for item in current.inputs:
                for producer in producers.get(item, []):
                    if producer.task_id not in ancestors[current.task_id] or producer.task_id in found:
                        continue
                    found.add(producer.task_id)
                    pending.append(producer)
        return [by_id[task_id] for task_id in sorted(found)
                if by_id[task_id].type in IMPLEMENTATION_TASK_TYPES and by_id[task_id].executor == "Hephaestus"]

    def authors_file(author, filename):
        path = PurePosixPath(_normalized(filename))
        for scope in author.write_scope:
            if _path_problem(scope):
                continue
            scoped = PurePosixPath(_normalized(scope))
            if path == scoped or (not scoped.suffix and path.is_relative_to(scoped)):
                return True
        return False

    if len(plan.tasks) > LOCAL_EXECUTION_CAPABILITIES["max_tasks"]:
        add("EXECUTION_TASK_LIMIT", plan.tasks[0],
            "Local execution allows at most 30 tasks per plan.",
            "Return a complete plan with at most 30 bounded tasks without dropping requirement coverage.")
    for task in plan.tasks:
        readonly = task.type in READ_ONLY_TASK_TYPES
        implementation = task.type in IMPLEMENTATION_TASK_TYPES
        if task.reviewer != "Argus" or not (
            (implementation and task.executor == "Hephaestus")
            or (readonly and task.executor in {"Momus", "Hermes"})
        ):
            add("UNSUPPORTED_EXECUTOR_PROFILE", task,
                "Task executor/reviewer roles do not match the registered local execution profile.",
                "Use Hephaestus for interface/implement, Momus or Hermes for test/integrate/doc, and Argus for independent review.")
        for scope in task.write_scope:
            problem = _path_problem(scope)
            if problem:
                add("UNSUPPORTED_EXECUTION_PATH", task,
                    f"write_scope {_label(scope)} is an unbounded or hidden/control path.",
                    "Use a bounded relative directory/file path; remove drive, traversal and hidden/control components.")
                continue
            suffix = PurePosixPath(_normalized(scope)).suffix
            if readonly and suffix:
                add("READ_ONLY_SCOPE_REQUIRES_DIRECTORY", task,
                    f"Read-only write_scope {_label(scope)} names a file instead of a report directory.",
                    "Use a report directory such as reports/task-id; frozen checks produce <directory>/check-report.json, not source or documentation files.")
            elif implementation and suffix and suffix not in IMPLEMENTATION_FILE_SUFFIXES:
                add("UNSUPPORTED_IMPLEMENTATION_FILE", task,
                    f"Implementation write_scope {_label(scope)} has an unsupported file suffix.",
                    "Use .py, .json, .md or .txt files, or a bounded directory; .pyi and typing-based stubs are unsupported.")

        authors = available_authors(task) if readonly else []
        reports = {str(PurePosixPath(_normalized(task.write_scope[0])) / "check-report.json")}
        for output in task.outputs:
            if not _file_claim(output):
                continue
            problem = _path_problem(output)
            suffix = PurePosixPath(_normalized(output)).suffix
            if problem:
                add("UNSUPPORTED_EXECUTION_PATH", task,
                    f"Output filename {_label(output)} is an unbounded or hidden/control path.",
                    "Use a logical artifact ID or a bounded supported file path.")
            elif implementation and suffix not in IMPLEMENTATION_FILE_SUFFIXES:
                add("UNSUPPORTED_IMPLEMENTATION_FILE", task,
                    f"Implementation output {_label(output)} has an unsupported file suffix.",
                    "Use .py, .json, .md or .txt for actual files; keep logical artifact IDs distinct from physical file paths.")
            elif readonly and str(PurePosixPath(_normalized(output))) not in reports and not any(
                authors_file(author, output) for author in authors
            ):
                add("READ_ONLY_OUTPUT_HAS_NO_AUTHOR", task,
                    f"Read-only output {_label(output)} has no declared ancestor file author.",
                    "Use a logical output ID or the generated <report-directory>/check-report.json; required source/docs files need a bounded Hephaestus ancestor supplied through inputs. Momus frozen acceptance already supplies executable tests.")
    # Keep deterministic ordering and avoid repeated identical diagnoses.
    return list({(issue["code"], issue["task_id"], issue["message"]): issue for issue in issues}.values())

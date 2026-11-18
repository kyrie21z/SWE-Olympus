"""Independent, inspectable acceptance definitions produced before implementation."""

import ast

from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.agents.runtime import OutputSchemaError, RolePermissions, RoleRunInput
from olympus.contracts.models import Task, fingerprint
from olympus.execution.policy import FORBIDDEN_NAMES


def validate_check(source: str, scopes: list[str]) -> None:
    """A bounded trusted-local test profile, not a hostile-code OS sandbox."""
    if not isinstance(source, str) or not source.strip() or len(source.encode()) > 65536:
        raise OutputSchemaError("acceptance script must contain at most 64 KB of Python")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise OutputSchemaError("acceptance script is not valid Python") from exc
    roots = {scope.replace("\\", "/").split("/")[0].removesuffix(".py") for scope in scopes}
    allowed = {"unittest", "math", "threading", "concurrent", *roots}
    assertions = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if isinstance(node, ast.ImportFrom) and node.level:
                raise OutputSchemaError("acceptance uses an unregistered relative import")
            if any(name.split(".")[0] not in allowed for name in names):
                raise OutputSchemaError("acceptance uses an unregistered module")
            if any(a.name.startswith("_") or a.name == "*" for a in node.names):
                raise OutputSchemaError("acceptance uses a private or wildcard import")
        if isinstance(node, ast.Name) and (
            node.id in FORBIDDEN_NAMES or (node.id.startswith("__") and node.id != "__name__")
        ):
            raise OutputSchemaError("acceptance uses a privileged builtin")
        if isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in {
            "os", "sys", "subprocess", "socket", "builtins", "importlib", "pathlib", "shutil", "ctypes",
        }):
            raise OutputSchemaError("acceptance uses private reflection")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "__" in node.value and node.value != "__main__":
                raise OutputSchemaError("acceptance uses an introspection string")
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant):
                raise OutputSchemaError("acceptance assertion cannot be an unconditional constant")
            assertions += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assertions += int(node.func.attr.startswith("assert"))
    if not assertions:
        raise OutputSchemaError("acceptance must include actual assertions")


def generate_acceptance(runner, requirement, design, plan) -> dict[str, dict]:
    """Momus receives approved semantics, never implementation or its self-evaluation."""
    scopes = sorted({scope for task in plan.tasks for scope in task.write_scope})
    manifest = {
        "requirement_content": requirement.content,
        "design_content": design.content,
        "tasks": [task.model_dump(mode="json") for task in plan.tasks],
        "prompt": (
            "Momus: independently write executable Python acceptance checks BEFORE implementation. "
            "Return ONLY JSON {tasks:[{task_id:string,checks:{check_id:Python_source}}]}. "
            "Cover exactly every task and every task.checks entry. Use real assertions against the "
            "specified interfaces, edge cases and requirements, never assert True or author claims. "
            "The work directory contains this task's files and ancestor input files at write_scope paths. "
            "Import implementation modules by those paths. Test/integrate/doc tasks inspect ancestor "
            "outputs and do not author implementation. Imports allowed: unittest, math, threading, "
            "concurrent.futures, and implementation modules. No filesystem, network, processes, "
            "eval/exec, reflection or private attributes. No third-party dependencies. "
            "Use top-level assertions, or unittest.main() guarded by __name__ == '__main__'. "
            "Each implementation task must be executable with its own declared inputs; "
            "do not import code only produced by a future task. Return JSON strings for scripts."
        ),
    }

    def validate(output):
        rows = output.get("tasks")
        if not isinstance(rows, list) or len(rows) != len(plan.tasks):
            raise OutputSchemaError("acceptance must cover every task exactly once")
        found = {}
        tasks = {task.task_id: task for task in plan.tasks}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"task_id", "checks"}:
                raise OutputSchemaError("acceptance row needs task_id and checks")
            task = tasks.get(row["task_id"])
            if task is None or task.task_id in found or not isinstance(row["checks"], dict):
                raise OutputSchemaError("unknown or duplicate acceptance task")
            for script in row["checks"].values():
                validate_check(script, scopes)
            found[task.task_id] = FrozenAcceptance.freeze(task, row["checks"]).model_dump(mode="json")
        return found

    result = runner.run(RoleRunInput(
        role_id="Momus", task_kind="acceptance_definition", task_id=plan.object_id,
        logical_task_id=f"acceptance-{plan.object_id[:100]}",
        input_manifest=manifest, input_refs=[requirement.ref(), design.ref(), plan.ref()],
        idempotency_key="acceptance-" + fingerprint({"plan": plan.ref().model_dump(mode="json")}),
        permissions=RolePermissions(role_id="Momus", allowed_task_kinds=["acceptance_definition"],
                                    allowed_input_keys=list(manifest)),
    ), output_validator=validate)
    return validate(result.structured_output)


def validate_executable_plan(plan) -> None:
    if len(plan.tasks) > 30:
        raise OutputSchemaError("local workflow allows at most 30 tasks per plan")
    for raw in plan.tasks:
        task = Task.model_validate(raw.model_dump(mode="json"))
        implementation = task.executor == "Hephaestus" and task.type in {"interface", "implement"}
        read_only = task.executor in {"Momus", "Hermes"} and task.type in {"test", "integrate", "doc"}
        if task.reviewer != "Argus" or not (implementation or read_only):
            raise OutputSchemaError("plan must use the registered local Python executor/reviewer profile")

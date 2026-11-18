import ast
from pathlib import PurePosixPath
import re

from olympus.agents.runtime import AgentRunner, OutputSchemaError, RolePermissions, RoleRunInput
from olympus.agents.runtime.models import RunnerBudget
from olympus.contracts.models import Ref, Task, fingerprint


def _requires_import_free_interface(task: Task) -> bool:
    if task.type != "interface":
        return False
    # Enforce an explicit approved source-fact obligation, not a guess from
    # "no third-party dependencies" or a generic interface task type.
    empty_roots = re.compile(r"\bimport_roots\b\s*(?:is|must be|==|=|:|必须为|为空|为)?\s*(?:empty|\(\)|\[\]|空)", re.I)
    return any(empty_roots.search(check) for check in task.checks)


def _python_policy_hint(error: SyntaxError | ValueError) -> str:
    if isinstance(error, SyntaxError):
        return f"Python 代码第 {error.lineno or 1} 行存在语法错误；修正语法、缩进和括号后返回完整 changes。"
    reason = str(error)
    if "private/introspection attributes" in reason:
        return (
            "实现代码禁止任何下划线开头的属性访问，包括 self._records、self._helper() 和双下划线反射。"
            "改用 self.records、self.lock 等公开存储属性；辅助函数放在模块级并使用公开名称。"
            "若契约限定类的公开方法数量，不要给类新增辅助方法。返回完整 changes。"
        )
    if "import" in reason:
        return (
            "实现代码仅允许 import math 和 from threading import Lock/RLock；"
            "不要导入 typing、re、unittest、其他本地模块或第三方包，不使用相对、私有或通配导入。"
            "需要字符串格式校验时使用内置字符串操作和明确的 ASCII 字符集合。返回完整 changes。"
        )
    if "introspection strings" in reason:
        return "实现源码字符串不得包含双下划线反射名称；使用公开接口和普通数据，删除反射字符串后返回完整 changes。"
    return (
        "实现代码禁止动态执行、反射、私有名称及特权内置函数；"
        "不要使用 getattr、setattr、vars、globals、open、eval、exec 或双下划线标识符。"
        "改用公开存储、公开调用和普通 Python 数据后返回完整 changes。"
    )


class HephaestusImplementer:
    def __init__(self, runner: AgentRunner):
        self.runner = runner

    def propose(self, task: Task, inputs: list[Ref], *, run_id: str, generation: int,
                context: dict | None = None, budget: RunnerBudget | None = None):
        import_free = _requires_import_free_interface(task)
        schema = {
            "type": "object", "additionalProperties": False, "required": ["changes"], "properties": {
                "changes": {"type": "array", "minItems": 1, "maxItems": 30, "items": {
                    "type": "object", "additionalProperties": False, "required": ["path", "content"],
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                }},
            },
        }
        manifest = {
            "task": task.model_dump(mode="json"),
            "inputs": [r.model_dump(mode="json") for r in inputs],
            "input_content": context or {},
            "output_schema": schema,
            "prompt": (
                "Hephaestus: implement only this authorized local Python task. Return JSON "
                "{changes:[{path:relative_path,content:UTF8_source}]}. Use pure Python functions "
                "and data, optionally import math or from threading import Lock/RLock; "
                "do not import typing, re, unittest, other local modules or third-party packages. "
                "Use public storage attributes such as self.records and self.lock. ANY attribute "
                "starting with an underscore is forbidden, including self._x, self._helper() and "
                "dunder reflection. Use public module-level helper functions; if the contract fixes "
                "the class's six public methods, do not add class helper methods. Use built-in string "
                "operations and explicit ASCII character membership for regex-style ID validation; "
                "no OS/filesystem/network/process APIs, dynamic "
                "evaluation or introspection. Only write inside task.write_scope. You cannot edit "
                "frozen acceptance scripts, expected values or fixtures; do not output review, "
                "confirmation, execution status, hashes or claims of successful verification."
            ),
        }
        if all(PurePosixPath(scope).suffix in {".md", ".txt"} for scope in task.write_scope):
            manifest["prompt"] = (
                "Hephaestus: author only the documentation files in task.write_scope. Return JSON "
                "{changes:[{path:relative_path,content:UTF8_document_text}]}. Write actual readable "
                "Markdown or plain text, following task.goal/checks and the supplied accepted source "
                "and execution evidence. Use exact public API names and runnable command arguments "
                "from that evidence. Report measured counts only when supplied by actual execution. "
                "Do not invent results, edit frozen checks or return review, approval or status."
            )
        if task.type == "interface":
            imports = ("No imports: the approved checks require empty import_roots. " if import_free else
                       "Avoid imports unless required by this task; only math or from threading import Lock/RLock are registered. ")
            manifest["prompt"] = (
                "Hephaestus: return JSON {changes:[{path:relative_path,content:UTF8_source}]}. "
                "Declare only the public class/functions and exact method signatures required by task.goal/checks, "
                "with required docstrings and built-in type annotations. Use pass or ellipsis bodies. "
                "Give every interface class AND every declared method its own docstring containing the exact "
                "marker Interface contract. Include the required return schema in each method docstring. "
                "Do not implement runtime behavior, constructors, storage/state, locks, validation helpers, "
                "or extra public methods; later implement tasks own these. "
                + imports +
                "No typing, re, unittest, private attributes such as self._x, reflection or system access. "
                "Write only task.write_scope; preserve all required documentation and response-schema markers. "
                "Do not edit frozen checks/fixtures or return status, review or approval."
            )
        if task.api_contracts:
            manifest["prompt"] += (
                " Honor task.api_contracts exactly: declare its named classes and public methods in the "
                "specified files. Preserve the constructor protocol in task.checks. Do not substitute "
                "abbreviations or add aliases to satisfy misspelled calls."
            )
        manifest["prompt"] += (
            " If input_content.previous_attempt is supplied, use its actual failed checks and prior source "
            "to fix the specific failure while preserving valid behavior. Frozen checks remain immutable."
        )
        def validate(output):
            changes = output.get("changes")
            if set(output) != {"changes"} or not isinstance(changes, list) or not changes:
                raise OutputSchemaError("implementation must contain a nonempty changes array")
            if len(changes) > 30:
                raise OutputSchemaError("too many changed files")
            for change in changes:
                if (not isinstance(change, dict) or set(change) != {"path", "content"} or
                        not isinstance(change["path"], str) or not isinstance(change["content"], str)):
                    raise OutputSchemaError("each change requires path and UTF-8 source content")
                if len(change["content"].encode()) > 262144:
                    raise OutputSchemaError("file exceeds the local adapter limit")
                if PurePosixPath(change["path"].replace("\\", "/")).suffix == ".py":
                    # The execution package imports this implementer; defer this
                    # shared-policy import until module initialization finishes.
                    from olympus.execution.policy import validate_python

                    try:
                        validate_python(change["content"])
                    except (SyntaxError, ValueError) as exc:
                        raise OutputSchemaError(
                            "implementation Python violates the local execution policy",
                            hint=_python_policy_hint(exc),
                        ) from exc
                    if import_free and any(isinstance(node, (ast.Import, ast.ImportFrom))
                                           for node in ast.walk(ast.parse(change["content"]))):
                        raise OutputSchemaError(
                            "interface declarations violate the explicit empty-import contract",
                            hint="此接口任务明确要求 source_metadata import_roots 为空；删除全部 import，包括 threading.Lock。"
                                 "只返回要求的类、公开方法签名、文档字符串和内置类型注解，方法体使用 pass 或 ...。"
                                 "不要生成构造函数、运行时存储、锁或校验辅助函数；这些属于后续 implement 任务。返回完整 changes。",
                        )
                    if task.api_contracts:
                        from olympus.workflows.api_contracts import validate_implementation_contracts

                        validate_implementation_contracts(
                            change["content"], change["path"], task.api_contracts,
                        )
        request = RoleRunInput(
            role_id="Hephaestus", task_kind="implement", logical_task_id=f"exec-{task.task_id}-g{generation}",
            task_id=task.task_id, run_id=run_id, generation=generation,
            idempotency_key="p4-" + fingerprint({"run": run_id, "role": "Hephaestus",
                "task": task.content_hash, "generation": generation,
                "inputs": [ref.model_dump(mode="json") for ref in inputs]}),
            fixture_key=f"Hephaestus:{task.task_id}",
            input_manifest=manifest, input_refs=inputs,
            output_schema=schema,
            permissions=RolePermissions(role_id="Hephaestus", allowed_task_kinds=["implement"],
                                        allowed_input_keys=list(manifest),
                                        allowed_write_scopes=list(task.write_scope)),
            budget=budget or RunnerBudget(),
        )
        return self.runner.run(request, output_validator=validate)

from olympus.agents.runtime import AgentRunner, OutputSchemaError, RolePermissions, RoleRunInput
from olympus.agents.runtime.models import RunnerBudget
from olympus.contracts.models import Ref, Task, fingerprint


class HephaestusImplementer:
    def __init__(self, runner: AgentRunner):
        self.runner = runner

    def propose(self, task: Task, inputs: list[Ref], *, run_id: str, generation: int,
                context: dict | None = None, budget: RunnerBudget | None = None):
        manifest = {
            "task": task.model_dump(mode="json"),
            "inputs": [r.model_dump(mode="json") for r in inputs],
            "input_content": context or {},
            "prompt": (
                "Hephaestus: implement only this authorized local Python task. Return JSON "
                "{changes:[{path:relative_path,content:UTF8_source}]}. Use pure Python functions "
                "and data, optionally import math or from threading import Lock/RLock; "
                "no OS/filesystem/network/process APIs, dynamic "
                "evaluation or introspection. Only write inside task.write_scope. You cannot edit "
                "frozen acceptance scripts, expected values or fixtures; do not output review, "
                "confirmation, execution status, hashes or claims of successful verification."
            ),
        }
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
        request = RoleRunInput(
            role_id="Hephaestus", task_kind="implement", logical_task_id=f"exec-{task.task_id}-g{generation}",
            task_id=task.task_id, run_id=run_id, generation=generation,
            idempotency_key="p4-" + fingerprint({"run": run_id, "role": "Hephaestus",
                "task": task.content_hash, "generation": generation,
                "inputs": [ref.model_dump(mode="json") for ref in inputs]}),
            fixture_key=f"Hephaestus:{task.task_id}",
            input_manifest=manifest, input_refs=inputs,
            permissions=RolePermissions(role_id="Hephaestus", allowed_task_kinds=["implement"],
                                        allowed_input_keys=list(manifest),
                                        allowed_write_scopes=list(task.write_scope)),
            budget=budget or RunnerBudget(),
        )
        return self.runner.run(request, output_validator=validate)

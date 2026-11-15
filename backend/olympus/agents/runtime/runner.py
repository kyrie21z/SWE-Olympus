"""One runner for every role: validate scope, reserve calls, audit output, record evidence."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from pydantic import JsonValue, ValidationError

from olympus.agents.runtime.models import (
    InputWhitelistError,
    OutputSchemaError,
    RolePermissions,
    RoleResult,
    RoleRunInput,
    RoleUsage,
    RunnerPermissionError,
    UnknownOutcomeError,
)
from olympus.agents.runtime.providers import MockProvider, RealProvider
from olympus.agents.runtime.records import (
    ExecutionRecordService,
    strip_internal_reasoning,
)
from olympus.contracts.models import Artifact, ExecutionRecord, fingerprint
from olympus.requests.ledger import LedgerEntry, RequestLedger, hash_refs

_PATH_KEYS = frozenset({"path", "location", "file", "files", "target_path", "write_scope"})
_SOFT_PATH_KEYS = frozenset({"files"})
_TYPE_CHECKS: dict[str, Callable[[object], bool]] = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
    "null": lambda value: value is None,
}


def _clip(value: str, limit: int = 160) -> str:
    return value[:limit]


class AgentRunner:
    """Executes role requests through any provider with the same safety envelope."""

    def __init__(
        self,
        provider: MockProvider | RealProvider,
        ledger: RequestLedger | None = None,
        records: ExecutionRecordService | None = None,
    ):
        self.provider = provider
        self.ledger = ledger if ledger is not None else RequestLedger()
        self.records = records if records is not None else ExecutionRecordService()

    def run(
        self,
        request: RoleRunInput,
        *,
        prompt_builder: Callable[[dict[str, JsonValue]], Any] | None = None,
        output_validator: Callable[[dict[str, JsonValue]], Any] | None = None,
    ) -> RoleResult:
        self._validate_request(request)
        input_hash = hash_refs(request.input_refs)
        attempt_id = self._attempt_id(request, input_hash)
        base_key = request.idempotency_key or self._base_idempotency_key(request, input_hash)
        prompt: Any = prompt_builder(request.input_manifest) if prompt_builder else request.input_manifest

        require_configured = getattr(self.provider, "require_configured", None)
        if require_configured is not None:
            require_configured()

        steps = self.provider.steps(request)
        tool_payloads: list[dict[str, JsonValue]] = []
        output: dict[str, JsonValue] = {}
        prompt_tokens = 0
        completion_tokens = 0
        elapsed_ms = 0
        unknown_usage = False
        call_count = 0
        last_entry: LedgerEntry | None = None

        for index, step in enumerate(steps, start=1):
            if request.budget.calls_used + call_count >= request.budget.max_calls:
                raise RunnerPermissionError("call budget exhausted")
            kind = step.get("kind", "model")
            key = self._step_key(base_key, index, len(steps))
            entry = self.ledger.prepare(
                attempt_id=attempt_id,
                logical_task_id=request.logical_task_id,
                run_id=request.run_id,
                task_id=request.task_id,
                role_id=request.role_id,
                task_kind=request.task_kind,
                input_hash=input_hash,
                idempotency_key=key,
                generation=request.generation,
                step_index=index,
                kind=kind if kind in ("model", "tool") else "model",
                mode=self.provider.mode,
            )
            entry = self.ledger.mark_sent(entry.request_id)
            result = self.provider.invoke(request, step, step_index=index, prompt=prompt)
            call_count += 1
            if result.provider_request_id is not None:
                entry = self.ledger.mark_sent(entry.request_id, result.provider_request_id)
            if result.late_generation is not None:
                entry = self.ledger.record_outcome(
                    entry.request_id, "succeeded", current_generation=result.late_generation
                )
            else:
                entry = self.ledger.record_outcome(
                    entry.request_id,
                    result.status,
                    response_summary=(
                        {"output_keys": sorted(result.output)} if result.output is not None else None
                    ),
                    error_code=result.error_code,
                )
            last_entry = entry
            if result.usage:
                prompt_tokens += result.usage.get("prompt_tokens", 0)
                completion_tokens += result.usage.get("completion_tokens", 0)
                elapsed_ms += result.usage.get("elapsed_ms", 0)
            else:
                unknown_usage = True
            if result.tool is not None:
                tool_payloads.append(result.tool)
            if result.output is not None:
                output = result.output

            if entry.late_response_ignored:
                self._record_failure(
                    request,
                    attempt_id,
                    tool_payloads,
                    call_count,
                    elapsed_ms,
                    "LATE_RESPONSE_IGNORED",
                )
                raise UnknownOutcomeError(f"late response ignored for request {entry.request_id}")
            if result.status == "outcome_unknown":
                self._record_failure(
                    request,
                    attempt_id,
                    tool_payloads,
                    call_count,
                    elapsed_ms,
                    "OUTCOME_UNKNOWN",
                )
                raise UnknownOutcomeError(f"outcome unknown for request {entry.request_id}")
            if result.status == "failed":
                self._record_failure(
                    request,
                    attempt_id,
                    tool_payloads,
                    call_count,
                    elapsed_ms,
                    f"PROVIDER_FAILED:{result.error_code or 'unknown'}",
                )
                raise RuntimeError(f"provider request failed: {result.error_code or 'unknown'}")

        if last_entry is None:
            raise ValueError("provider produced no steps")

        sanitized_output = strip_internal_reasoning(output)
        output = sanitized_output if isinstance(sanitized_output, dict) else {}

        try:
            self._audit_output(output, request.permissions)
            self._validate_schema(output, request.output_schema)
            if output_validator is not None:
                try:
                    verdict = output_validator(output)
                except OutputSchemaError:
                    raise
                except Exception as exc:
                    raise OutputSchemaError(f"output validator failed: {exc}") from exc
                if verdict is False:
                    raise OutputSchemaError("output validator rejected output")
            artifacts = self._parse_artifacts(output)
            findings = self._findings(output)
            issues = self._issues(output)
            source_refs = self._source_refs(output)
        except (RunnerPermissionError, OutputSchemaError) as exc:
            self._record_failure(
                request,
                attempt_id,
                tool_payloads,
                call_count,
                elapsed_ms,
                f"{type(exc).__name__.upper()}:{exc}",
            )
            if isinstance(exc, RunnerPermissionError):
                raise RunnerPermissionError(str(exc)) from exc
            raise OutputSchemaError(str(exc)) from exc

        attempt = self.ledger.to_attempt(last_entry.request_id)
        saved = self._save_record(
            request=request,
            attempt_id=attempt_id,
            tools=tool_payloads,
            artifacts=artifacts,
            output=output,
            call_count=call_count,
            elapsed_ms=elapsed_ms,
            check_outputs=self._check_outputs(output),
        )
        return RoleResult(
            role_id=request.role_id,
            task_kind=request.task_kind,
            logical_task_id=request.logical_task_id,
            task_id=request.task_id,
            findings=findings,
            artifacts=artifacts,
            structured_output=output,
            source_refs=source_refs,
            issues=issues,
            usage=RoleUsage(
                call_count=call_count,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                elapsed_ms=elapsed_ms,
                unknown_usage=unknown_usage,
            ),
            attempt=attempt,
            execution_record_id=saved.record_id,
            mode=self.provider.mode,
        )

    # ------------------------------------------------------------------ validation
    @staticmethod
    def _validate_request(request: RoleRunInput) -> None:
        if request.permissions.can_propose_task_scopes and (
            request.role_id != "Daedalus" or request.task_kind != "plan"
        ):
            raise RunnerPermissionError("only the Daedalus plan task may propose task scopes")
        if request.permissions.role_id != request.role_id:
            raise RunnerPermissionError("permissions do not belong to the requesting role")
        if request.task_kind not in request.permissions.allowed_task_kinds:
            raise RunnerPermissionError(
                f"role {request.role_id!r} may not run task kind {request.task_kind!r}"
            )
        unknown = sorted(set(request.input_manifest) - set(request.permissions.allowed_input_keys))
        if unknown:
            raise InputWhitelistError("input keys outside whitelist: " + ", ".join(unknown))
        if not request.input_refs:
            raise InputWhitelistError("input_refs must be non-empty")

    # ------------------------------------------------------------------ security
    def _audit_output(
        self, value: JsonValue, permissions: RolePermissions, *, trail: tuple[str, ...] = ()
    ) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = key.lower()
                if lowered in {"confirmation", "confirmed_by"} and item not in (
                    None,
                    "",
                    [],
                    {},
                    False,
                ):
                    raise RunnerPermissionError(f"output attempts privileged write: {key}")
                if lowered in {"call_budget", "calls_used"} and item is not None:
                    raise RunnerPermissionError(f"output attempts privileged write: {key}")
                if lowered == "state" and item == "accepted":
                    raise RunnerPermissionError("output attempts to mark task accepted")
                if lowered == "verification_status" and item == "passed":
                    raise RunnerPermissionError("output attempts to mark verification passed")
                if lowered in _PATH_KEYS:
                    for requested_path in self._path_strings(item):
                        normalized = self._assert_safe_relative(requested_path)
                        proposed_scope = (
                            permissions.can_propose_task_scopes
                            and len(trail) == 2
                            and trail[0] == "tasks"
                            and trail[1].isdigit()
                            and lowered == "write_scope"
                        )
                        # A bare file list may describe read evidence; it is scope
                        # checked only when the role declared write scopes.
                        if not proposed_scope and (
                            permissions.allowed_write_scopes or lowered not in _SOFT_PATH_KEYS
                        ):
                            self._assert_in_scope(normalized, permissions.allowed_write_scopes)
                self._audit_output(item, permissions, trail=(*trail, key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self._audit_output(item, permissions, trail=(*trail, str(index)))

    @staticmethod
    def _path_strings(value: JsonValue) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
        return []

    @staticmethod
    def _assert_safe_relative(path: str) -> str:
        normalized = path.replace("\\", "/").strip()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        candidate = PurePosixPath(normalized)
        if (
            not normalized
            or normalized.startswith("/")
            or candidate.is_absolute()
            or ".." in candidate.parts
            or ":" in normalized
        ):
            raise RunnerPermissionError(f"write outside allowed scopes: {path}")
        return normalized

    @classmethod
    def _assert_in_scope(cls, path: str, scopes: list[str]) -> None:
        normalized = cls._assert_safe_relative(path)
        for scope in scopes:
            normalized_scope = scope.replace("\\", "/").strip().rstrip("/")
            if normalized == normalized_scope or normalized.startswith(normalized_scope + "/"):
                return
        raise RunnerPermissionError(f"write outside allowed scopes: {path}")

    # ------------------------------------------------------------------ output checks
    @staticmethod
    def _validate_schema(output: dict[str, JsonValue], schema: dict[str, JsonValue]) -> None:
        if not schema:
            return
        expected_type = schema.get("type")
        if expected_type == "object" and not isinstance(output, dict):
            raise OutputSchemaError("output must be an object")
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if key not in output:
                    raise OutputSchemaError(f"output missing required field: {key}")
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, spec in properties.items():
                if key not in output or not isinstance(spec, dict):
                    continue
                declared = spec.get("type")
                if (
                    isinstance(declared, str)
                    and declared in _TYPE_CHECKS
                    and not _TYPE_CHECKS[declared](output[key])
                ):
                    raise OutputSchemaError(f"output field {key} must be of type {declared}")

    @staticmethod
    def _parse_artifacts(output: dict[str, JsonValue]) -> list[Artifact]:
        raw = output.get("artifacts", [])
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise OutputSchemaError("artifacts must be a list")
        artifacts: list[Artifact] = []
        for item in raw:
            if isinstance(item, Artifact):
                artifacts.append(item)
            elif isinstance(item, dict):
                try:
                    artifacts.append(Artifact.model_validate(item))
                except ValidationError as exc:
                    raise OutputSchemaError(f"invalid artifact: {exc}") from exc
            else:
                raise OutputSchemaError("artifact must be an object")
        return artifacts

    @staticmethod
    def _findings(output: dict[str, JsonValue]) -> list[str]:
        raw = output.get("findings", [])
        if not isinstance(raw, list):
            raise OutputSchemaError("findings must be a list")
        return [item if isinstance(item, str) else str(item) for item in raw]

    @staticmethod
    def _issues(output: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
        raw = output.get("issues", [])
        if not isinstance(raw, list):
            raise OutputSchemaError("issues must be a list")
        issues: list[dict[str, JsonValue]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise OutputSchemaError("each issue must be an object")
            issues.append(item)
        return issues

    @staticmethod
    def _source_refs(output: dict[str, JsonValue]) -> list[str]:
        raw = output.get("source_refs", [])
        if not isinstance(raw, list):
            raise OutputSchemaError("source_refs must be a list")
        return [item if isinstance(item, str) else str(item) for item in raw]

    @staticmethod
    def _check_outputs(output: dict[str, JsonValue]) -> list[str]:
        raw = output.get("check_outputs", [])
        if not isinstance(raw, list):
            return []
        return [item if isinstance(item, str) else str(item) for item in raw]

    # ------------------------------------------------------------------ records
    def _save_record(
        self,
        *,
        request: RoleRunInput,
        attempt_id: str,
        tools: list[dict[str, JsonValue]],
        artifacts: list[Artifact],
        output: dict[str, JsonValue],
        call_count: int,
        elapsed_ms: int,
        check_outputs: list[str],
    ) -> ExecutionRecord:
        diff = output.get("diff")
        record = ExecutionRecord(
            record_id=f"ER-{attempt_id}",
            attempt_id=attempt_id,
            task_id=request.task_id,
            inputs=request.input_refs,
            tools=tools,
            artifacts=[artifact.artifact_id for artifact in artifacts],
            diff=diff if isinstance(diff, str) else "",
            check_outputs=check_outputs,
            elapsed_ms=elapsed_ms,
            call_count=call_count,
            retry_history=[],
            mode=self.provider.mode,
        )
        return self.records.record(
            record,
            role_id=request.role_id,
            logical_task_id=request.logical_task_id,
            run_id=request.run_id,
        )

    def _record_failure(
        self,
        request: RoleRunInput,
        attempt_id: str,
        tools: list[dict[str, JsonValue]],
        call_count: int,
        elapsed_ms: int,
        note: str,
    ) -> None:
        self._save_record(
            request=request,
            attempt_id=attempt_id,
            tools=tools,
            artifacts=[],
            output={},
            call_count=call_count,
            elapsed_ms=elapsed_ms,
            check_outputs=[note],
        )

    # ------------------------------------------------------------------ keys
    @staticmethod
    def _attempt_id(request: RoleRunInput, input_hash: str) -> str:
        seed = {
            "run_id": request.run_id,
            "task_id": request.task_id,
            "logical_task_id": request.logical_task_id,
            "role_id": request.role_id,
            "generation": request.generation,
            "input_hash": input_hash,
            "idempotency_key": request.idempotency_key,
        }
        return f"ATT-{fingerprint(seed)[:24]}"

    @staticmethod
    def _base_idempotency_key(request: RoleRunInput, input_hash: str) -> str:
        return _clip(f"{request.role_id}:{request.logical_task_id}:{request.generation}:{input_hash[:24]}")

    @staticmethod
    def _step_key(base: str, index: int, total: int) -> str:
        if total == 1:
            return _clip(base)
        return _clip(f"{base[:145]}:step{index}")

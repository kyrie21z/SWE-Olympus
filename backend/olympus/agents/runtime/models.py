"""Role-level runtime contracts: what a role may do, and what it produced."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from olympus.contracts.models import (
    Artifact,
    Attempt,
    Contract,
    Id,
    Mode,
    NonNegative,
    Positive,
    Ref,
)


class RunnerPermissionError(PermissionError):
    """Role attempted an action outside its declared permissions."""


class InputWhitelistError(ValueError):
    """Input manifest contains keys the role may not read, or no input refs at all."""


class OutputSchemaError(ValueError):
    """Model output does not satisfy the declared schema or validator."""

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.hint = hint  # Only program-authored diagnostics, never model output.
        self.role_id: str | None = None


class ProviderNotConfiguredError(RuntimeError):
    """Real provider credentials/config are incomplete."""


class UnknownOutcomeError(RuntimeError):
    """Provider accepted a request but its outcome cannot be established."""


class RolePermissions(Contract):
    role_id: Id
    allowed_task_kinds: list[Id]
    allowed_input_keys: list[Id]
    allowed_write_scopes: list[Id] = Field(default_factory=list)
    # A PLAN may propose future task scopes; this grants no file/tool write permission.
    can_propose_task_scopes: bool = False
    can_write_confirmation: Literal[False] = False
    can_write_budget: Literal[False] = False
    can_mark_accepted: Literal[False] = False


class RunnerBudget(Contract):
    max_calls: Positive = 10
    calls_used: NonNegative = 0
    timeout_seconds: Positive = 120


class RoleRunInput(Contract):
    role_id: Id
    task_kind: Id
    logical_task_id: Id
    run_id: Id = "RUN-default"
    task_id: Id
    generation: Positive = 1
    input_manifest: dict[str, JsonValue]
    input_refs: list[Ref]
    output_schema: dict[str, JsonValue] = Field(default_factory=dict)
    permissions: RolePermissions
    budget: RunnerBudget = Field(default_factory=RunnerBudget)
    idempotency_key: Id | None = None
    fixture_key: str | None = None


class RoleUsage(Contract):
    call_count: NonNegative
    prompt_tokens: NonNegative = 0
    completion_tokens: NonNegative = 0
    elapsed_ms: NonNegative = 0
    unknown_usage: bool = False


class RoleResult(Contract):
    role_id: Id
    task_kind: Id
    logical_task_id: Id
    task_id: Id
    findings: list[str] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    structured_output: dict[str, JsonValue] = Field(default_factory=dict)
    source_refs: list[Id] = Field(default_factory=list)
    issues: list[dict[str, JsonValue]] = Field(default_factory=list)
    usage: RoleUsage
    attempt: Attempt
    execution_record_id: Id
    mode: Mode

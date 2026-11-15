from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator
from typing_extensions import TypedDict

Id = Annotated[str, Field(min_length=1, max_length=160)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Positive = Annotated[int, Field(strict=True, ge=1)]
NonNegative = Annotated[int, Field(strict=True, ge=0)]
Mode = Literal["mock", "real_tool", "real_model"]
TaskState = Literal["waiting", "ready", "running", "verifying", "accepted", "failed", "blocked", "cancelled"]
Verification = Literal["unverified", "passed", "failed", "stale"]


def fingerprint(value: JsonValue) -> str:
    """Hash explicit semantic data only; no global recursive key stripping."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, json_schema_serialization_defaults_required=True
    )
    schema_version: Literal["1.0"] = "1.0"


class Ref(Contract):
    object_id: Id
    version: Positive
    content_hash: Hash


class Provenance(Contract):
    kind: Literal["material", "human", "agent_hypothesis", "synthetic_fixture"]
    source_id: Id
    locator: Id


class Confirmation(Contract):
    confirmation_id: Id
    actor: Id
    origin: Literal["human", "synthetic_fixture"]
    target: Ref
    dependencies: list[Ref]
    confirmed_at: AwareDatetime


class Versioned(Contract):
    object_id: Id
    version: Positive
    content_hash: Hash
    provenance: list[Provenance] = Field(min_length=1)
    # This explicitly delimits the semantic fingerprint domain. UI/runtime/signatures live outside it.
    content: dict[str, JsonValue]

    @model_validator(mode="after")
    def valid_hash(self):
        if self.content_hash != fingerprint(self.content):
            raise ValueError("content_hash does not match canonical semantic content")
        return self

    def ref(self) -> Ref:
        return Ref(object_id=self.object_id, version=self.version, content_hash=self.content_hash)


class RequirementItem(TypedDict):
    id: Id
    text: Id
    acceptance: Id


class RequirementContent(TypedDict):
    requirements: Annotated[list[RequirementItem], Field(min_length=1)]
    out_of_scope: list[Id]


class DesignElement(TypedDict):
    id: Id
    sources: Annotated[list[Id], Field(min_length=1)]
    description: Id
    acceptance_method: Id


class DesignContent(TypedDict):
    modules: Annotated[list[Id], Field(min_length=1)]
    data: list[Id]
    interfaces: list[Id]
    states: list[Id]
    concurrency: Id
    exceptions: list[Id]
    interaction: Id
    decisions: list[Id]
    elements: Annotated[list[DesignElement], Field(min_length=1)]


class Requirement(Versioned):
    content: RequirementContent
    kind: Literal["REQ"] = "REQ"
    confirmation: Confirmation | None = None

    @model_validator(mode="after")
    def requirement_fields(self):
        if not self.content.get("requirements") or "out_of_scope" not in self.content:
            raise ValueError("REQ requires requirements and out_of_scope")
        ids = [item["id"] for item in self.content["requirements"]]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate requirement ID")
        if self.confirmation and self.confirmation.target != self.ref():
            raise ValueError("confirmation target is stale")
        return self


class Design(Versioned):
    content: DesignContent
    kind: Literal["DS"] = "DS"
    requirement: Ref

    @model_validator(mode="after")
    def design_fields(self):
        for section in (
            "modules",
            "data",
            "interfaces",
            "states",
            "concurrency",
            "exceptions",
            "interaction",
            "decisions",
            "elements",
        ):
            if section not in self.content:
                raise ValueError(f"DS missing {section}")
        return self


class AttemptPolicy(Contract):
    max_auto_retries: Annotated[int, Field(strict=True, ge=0, le=2)] = 2
    timeout_seconds: Positive = 120


class Task(Contract):
    task_id: Id
    version: Positive
    content_hash: Hash
    type: Literal["interface", "implement", "test", "integrate", "doc"]
    goal: Id
    sources: list[Id] = Field(min_length=1)
    inputs: list[Id]
    outputs: list[Id] = Field(min_length=1)
    depends_on: list[Id]
    executor: Id
    reviewer: Id
    write_scope: list[Id] = Field(min_length=1)
    checks: list[Id] = Field(min_length=1)
    resources: list[Id]
    priority: NonNegative = 0
    policy: AttemptPolicy = Field(default_factory=AttemptPolicy)
    required: bool = True

    def semantic_content(self):
        return self.model_dump(mode="json", exclude={"schema_version", "content_hash", "version"})

    @model_validator(mode="after")
    def valid_task(self):
        if self.executor == self.reviewer:
            raise ValueError("executor cannot review its own task")
        if self.task_id in self.depends_on or len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("self or duplicate dependency")
        if self.content_hash != fingerprint(self.semantic_content()):
            raise ValueError("task hash mismatch")
        return self


class Plan(Versioned):
    kind: Literal["PLAN"] = "PLAN"
    requirement: Ref
    design: Ref
    tasks: list[Task] = Field(min_length=1)

    @model_validator(mode="after")
    def task_manifest(self):
        if len({t.task_id for t in self.tasks}) != len(self.tasks):
            raise ValueError("duplicate task ID")
        expected = [
            {"task_id": t.task_id, "version": t.version, "content_hash": t.content_hash} for t in self.tasks
        ]
        if self.content.get("tasks") != expected:
            raise ValueError("plan semantic content must bind task versions and hashes")
        return self


class TaskRuntime(Contract):
    task_id: Id
    state: TaskState = "waiting"
    verification_status: Verification = "unverified"
    disposition: Literal["none", "risk_accepted"] = "none"
    disposition_reason: str | None = None
    generation: Positive = 1

    @model_validator(mode="after")
    def evidence_status(self):
        if self.state == "accepted" and self.verification_status != "passed":
            raise ValueError("accepted requires passed verification")
        if self.disposition == "risk_accepted" and (
            not self.disposition_reason or self.state == "accepted" or self.verification_status == "passed"
        ):
            raise ValueError("risk acceptance is a reasoned disposition, not verified success")
        return self


class Run(Contract):
    run_id: Id
    mode: Mode
    approved_package: list[Ref]
    state_revision: NonNegative = 0
    event_seq: NonNegative = 0
    status: Literal["draft", "active", "paused", "awaiting_confirmation", "completed"] = "draft"
    call_budget: Positive = 40
    calls_used: NonNegative = 0
    tasks: list[TaskRuntime]

    @model_validator(mode="after")
    def valid_run(self):
        if self.calls_used > self.call_budget or len({t.task_id for t in self.tasks}) != len(self.tasks):
            raise ValueError("invalid budget or duplicate runtime task")
        if self.status == "completed" and any(t.state != "accepted" for t in self.tasks):
            raise ValueError("completed requires all runtime tasks accepted")
        return self


class Attempt(Contract):
    attempt_id: Id
    logical_task_id: Id
    run_id: Id
    task_id: Id
    generation: Positive
    input_hash: Hash
    provider_request_id: str | None
    idempotency_key: Id
    mode: Mode
    status: Literal["prepared", "sent", "succeeded", "failed", "outcome_unknown"]
    auto_retry_count: Annotated[int, Field(strict=True, ge=0, le=2)] = 0
    human_retry_count: NonNegative = 0


class Artifact(Contract):
    artifact_id: Id
    run_id: Id
    task_id: Id
    attempt_id: Id
    location: Id
    content_hash: Hash
    inputs: list[Ref]
    implementation_version: Id
    test_version: Id
    fixture_version: Id
    mode: Mode


class Review(Contract):
    review_id: Id
    target: Ref
    author: Id
    reviewer: Id
    verdict: Literal["passed", "blocked", "needs_revision"]
    findings: list[str]
    evidence: list[Id] = Field(min_length=1)
    mode: Mode

    @model_validator(mode="after")
    def independent(self):
        if self.author == self.reviewer:
            raise ValueError("reviewer must be independent of author")
        return self


class Command(Contract):
    operation_id: Id
    run_id: Id
    actor: Id
    action: Literal[
        "approve_design",
        "approve_plan",
        "start",
        "pause",
        "resume",
        "retry",
        "reassign",
        "accept_risk",
        "request_change",
        "confirm_delivery",
    ]
    target_id: Id
    expected_version: Positive
    expected_state_revision: NonNegative
    parameters: dict[str, JsonValue]


class Receipt(Contract):
    operation_id: Id
    run_id: Id
    status: Literal["accepted", "rejected", "pending"]
    reason_code: Id
    state_revision: NonNegative


class Event(Contract):
    event_id: Id
    run_id: Id
    event_seq: Positive
    task_id: str | None
    attempt_id: str | None
    old_state: TaskState | None
    new_state: TaskState | None
    reason: Id
    actor_type: Literal["program", "agent", "human"]
    occurred_at: AwareDatetime
    state_revision: Positive
    # Immutable authoritative projection committed in the same transaction.
    snapshot: Run

    @model_validator(mode="after")
    def matches_snapshot(self):
        if (self.run_id, self.event_seq, self.state_revision) != (
            self.snapshot.run_id,
            self.snapshot.event_seq,
            self.snapshot.state_revision,
        ):
            raise ValueError("event and snapshot mismatch")
        if self.task_id:
            task = next((t for t in self.snapshot.tasks if t.task_id == self.task_id), None)
            if not task or task.state != self.new_state:
                raise ValueError("event task state mismatch")
        return self


class ExecutionRecord(Contract):
    record_id: Id
    attempt_id: Id
    task_id: Id
    inputs: list[Ref]
    tools: list[dict[str, JsonValue]]
    artifacts: list[Id]
    diff: str
    check_outputs: list[str]
    elapsed_ms: NonNegative
    call_count: NonNegative
    retry_history: list[Id]
    mode: Mode


class ContractBundle(Contract):
    requirement: Requirement
    design: Design
    plan: Plan
    run: Run
    attempt: Attempt
    artifact: Artifact
    review: Review
    event: Event
    command: Command
    receipt: Receipt
    execution_record: ExecutionRecord

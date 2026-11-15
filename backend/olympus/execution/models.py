from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field

from olympus.agents.momus.acceptance import FrozenAcceptance
from olympus.contracts.models import Artifact, Contract, Hash, Id, Ref, Task
from olympus.reviews.common import ReviewRecord


class ExecutionReceipt(Contract):
    run_id: Id
    attempt_id: Id
    task_id: Id
    generation: int = Field(ge=1)
    operation_id: Id
    operation_aliases: list[Id] = Field(default_factory=list)
    request_id: Id
    input_hash: Hash
    task_hash: Hash
    state: Literal["running", "succeeded", "failed", "outcome_unknown", "cancelled"] = "running"
    artifacts: list[Artifact] = Field(default_factory=list)
    checks_passed: list[Id] = Field(default_factory=list)
    check_outputs: list[dict] = Field(default_factory=list)
    file_hashes: dict[str, str] = Field(default_factory=dict)
    acceptance_hash: Hash
    evidence_id: Id
    model_mode: str = "mock"
    tool_mode: Literal["real_tool"] = "real_tool"
    tool_invocations: int = Field(default=0, ge=0)
    cancel_requested: bool = False
    cancel_confirmed: bool = False
    error: str | None = None
    diff: str = ""
    model_request_ids: list[Id] = Field(default_factory=list)
    review: ReviewRecord | None = None


@dataclass
class Job:
    task: Task
    inputs: list[Ref]
    acceptance: FrozenAcceptance
    receipt: ExecutionReceipt
    process: object = None
    done: object = None
    evidence_hashes: dict[str, str] = field(default_factory=dict)

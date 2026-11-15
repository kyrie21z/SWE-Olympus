"""Nine-group task cards projected from a planned task.

A card is planning metadata only: it is derived from an existing ``Task`` plus
the requirement and design it came from. Cards cannot change task state, do not
carry acceptance, and never trigger execution.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from olympus.contracts.models import (
    Contract,
    Design,
    Hash,
    Id,
    NonNegative,
    Plan,
    Positive,
    Requirement,
    Task,
)

# Interface-first: the dedicated test task owns real boundary/concurrency
# execution; every other card names the independent test owner instead.
TEST_OWNER = "Momus"
ON_FAILURE = "blocked_awaiting_human"


class TaskIdentity(Contract):
    task_id: Id
    version: Positive
    content_hash: Hash


class TaskObjective(Contract):
    type: Literal["interface", "implement", "test", "integrate", "doc"]
    goal: Id


class SourceTrace(Contract):
    req_sources: list[Id]
    ds_sources: list[Id]


class UpstreamContract(Contract):
    depends_on: list[Id]
    required_accepted_inputs: list[Id]


class TaskOwnership(Contract):
    executor: Id
    reviewer: Id
    test_owner: Id

    @model_validator(mode="after")
    def independent_review(self):
        if self.executor == self.reviewer:
            raise ValueError("executor cannot review its own task")
        return self


class OutputAndScope(Contract):
    outputs: list[Id] = Field(min_length=1)
    write_scope: list[Id] = Field(min_length=1)


class VerificationGate(Contract):
    program_checks: list[Id] = Field(min_length=1)
    argus_review_required: bool
    # Acceptance stays frozen behind program checks plus independent review.
    acceptance_frozen: bool


class SchedulingHint(Contract):
    resources: list[Id]
    priority: NonNegative
    required: bool


class RecoveryPolicy(Contract):
    timeout_seconds: Positive
    max_auto_retries: NonNegative
    on_failure: Id


def _split_sources(task: Task, req_ids: list[str], ds_ids: list[str]) -> tuple[list[str], list[str]]:
    req_sources: list[str] = []
    ds_sources: list[str] = []
    for source in task.sources:
        if source in req_ids or source.startswith("FR-"):
            req_sources.append(source)
        elif source in ds_ids or source.startswith("D-"):
            ds_sources.append(source)
        else:
            raise ValueError(f"task {task.task_id} source {source} is neither a requirement nor a design element")
    return req_sources, ds_sources


class TaskCard(Contract):
    identity: TaskIdentity
    objective: TaskObjective
    source_trace: SourceTrace
    upstream_contract: UpstreamContract
    ownership: TaskOwnership
    output_and_scope: OutputAndScope
    verification_gate: VerificationGate
    scheduling_hint: SchedulingHint
    recovery_policy: RecoveryPolicy

    @property
    def task_id(self) -> str:
        return self.identity.task_id

    @classmethod
    def from_task(cls, task: Task, req: Requirement, ds: Design) -> TaskCard:
        req_ids = [item["id"] for item in req.content["requirements"]]
        ds_ids = [element["id"] for element in ds.content["elements"]]
        req_sources, ds_sources = _split_sources(task, req_ids, ds_ids)
        return cls(
            identity=TaskIdentity(
                task_id=task.task_id, version=task.version, content_hash=task.content_hash
            ),
            objective=TaskObjective(type=task.type, goal=task.goal),
            source_trace=SourceTrace(req_sources=req_sources, ds_sources=ds_sources),
            upstream_contract=UpstreamContract(
                depends_on=list(task.depends_on), required_accepted_inputs=list(task.inputs)
            ),
            ownership=TaskOwnership(
                executor=task.executor,
                reviewer=task.reviewer,
                test_owner=task.executor if task.type == "test" else TEST_OWNER,
            ),
            output_and_scope=OutputAndScope(
                outputs=list(task.outputs), write_scope=list(task.write_scope)
            ),
            verification_gate=VerificationGate(
                program_checks=list(task.checks),
                argus_review_required=task.reviewer == "Argus",
                acceptance_frozen=True,
            ),
            scheduling_hint=SchedulingHint(
                resources=list(task.resources), priority=task.priority, required=task.required
            ),
            recovery_policy=RecoveryPolicy(
                timeout_seconds=task.policy.timeout_seconds,
                max_auto_retries=task.policy.max_auto_retries,
                on_failure=ON_FAILURE,
            ),
        )


def build_task_cards(plan: Plan, req: Requirement, ds: Design) -> list[TaskCard]:
    """Build one card per task, in plan order."""
    return [TaskCard.from_task(task, req, ds) for task in plan.tasks]

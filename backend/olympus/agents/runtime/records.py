"""Execution-record storage with reasoning stripping and API-ready projections."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue

from olympus.contracts.models import ExecutionRecord

REASONING_KEYS = frozenset({"reasoning", "thinking", "chain_of_thought", "internal_reasoning"})


def strip_internal_reasoning(value: JsonValue) -> JsonValue:
    """Recursively drop private chain-of-thought fields from provider payloads."""
    if isinstance(value, dict):
        return {
            key: strip_internal_reasoning(item)
            for key, item in value.items()
            if key.lower() not in REASONING_KEYS
        }
    if isinstance(value, list):
        return [strip_internal_reasoning(item) for item in value]
    return value


@dataclass(frozen=True)
class _StoredRecord:
    record: ExecutionRecord
    role_id: str
    logical_task_id: str
    run_id: str


class ExecutionRecordService:
    """In-memory index over canonical ExecutionRecords.

    Records are indexed by role, task, logical task, attempt and run so API layers
    never have to re-stitch role history from raw attempt rows.
    """

    def __init__(self) -> None:
        self._records: dict[str, _StoredRecord] = {}

    def record(
        self,
        record: ExecutionRecord,
        *,
        role_id: str = "unknown",
        logical_task_id: str | None = None,
        run_id: str | None = None,
    ) -> ExecutionRecord:
        data = record.model_dump(mode="json")
        data["tools"] = strip_internal_reasoning(data["tools"])
        sanitized = ExecutionRecord.model_validate(data)
        self._records[sanitized.record_id] = _StoredRecord(
            record=sanitized,
            role_id=role_id,
            logical_task_id=logical_task_id or sanitized.task_id,
            run_id=run_id or "RUN-default",
        )
        return sanitized

    def get_by_attempt(self, attempt_id: str) -> ExecutionRecord:
        for stored in self._records.values():
            if stored.record.attempt_id == attempt_id:
                return stored.record
        raise KeyError(attempt_id)

    def list_by_role(self, role_id: str, *, run_id: str | None = None) -> list[ExecutionRecord]:
        return [
            stored.record
            for stored in self._records.values()
            if stored.role_id == role_id and (run_id is None or stored.run_id == run_id)
        ]

    def list_by_task(self, task_id: str, *, run_id: str | None = None) -> list[ExecutionRecord]:
        return [
            stored.record
            for stored in self._records.values()
            if stored.record.task_id == task_id and (run_id is None or stored.run_id == run_id)
        ]

    def list_by_logical_task(self, logical_task_id: str) -> list[ExecutionRecord]:
        return [
            stored.record
            for stored in self._records.values()
            if stored.logical_task_id == logical_task_id
        ]

    def project_for_api(
        self,
        run_id: str | None = None,
        *,
        role_id: str | None = None,
        task_id: str | None = None,
    ) -> list[dict[str, JsonValue]]:
        projection: list[dict[str, JsonValue]] = []
        for stored in self._records.values():
            if run_id is not None and stored.run_id != run_id:
                continue
            if role_id is not None and stored.role_id != role_id:
                continue
            if task_id is not None and stored.record.task_id != task_id:
                continue
            projection.append(
                {
                    "role_id": stored.role_id,
                    "logical_task_id": stored.logical_task_id,
                    "record": stored.record.model_dump(mode="json"),
                }
            )
        return projection

    def __len__(self) -> int:
        return len(self._records)

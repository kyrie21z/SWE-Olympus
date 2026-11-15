"""Request ledger: every model/tool call is registered before it leaves the process.

The ledger is deliberately conservative about ambiguity. A request whose outcome is
unknown keeps its reserved call budget and stays reconcilable; it is never silently
rewritten to "failed" or accounted as a free call.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue

from olympus.contracts.models import (
    Attempt,
    Contract,
    Hash,
    Id,
    Mode,
    NonNegative,
    Positive,
    Ref,
    fingerprint,
)

RequestKind = Literal["model", "tool"]
LedgerRequestStatus = Literal[
    "prepared", "sent", "succeeded", "failed", "cancelled", "outcome_unknown"
]
OutcomeStatus = Literal["succeeded", "failed", "outcome_unknown"]
CancelHookStatus = Literal["cancelled", "unknown", "already_terminal"]


class LedgerEntry(Contract):
    request_id: Id
    idempotency_key: Id
    attempt_id: Id
    logical_task_id: Id
    run_id: Id
    task_id: Id
    role_id: Id
    task_kind: Id
    generation: Positive
    step_index: Positive = 1
    kind: RequestKind = "model"
    input_hash: Hash
    mode: Mode
    status: LedgerRequestStatus
    provider_request_id: str | None = None
    calls_reserved: Positive = 1
    calls_consumed: NonNegative = 0
    budget_held: bool = True
    cancel_requested: bool = False
    needs_reconciliation: bool = False
    late_response_ignored: bool = False
    late_response_reason: str | None = None
    resend_of_request_id: str | None = None
    error_code: str | None = None
    response_summary: dict[str, JsonValue] | None = None


class QueryResult(Contract):
    request_id: Id
    idempotency_key: Id
    attempt_id: Id
    status: LedgerRequestStatus
    is_terminal: bool
    needs_reconciliation: bool
    budget_held: bool
    provider_request_id: str | None = None
    late_response_ignored: bool = False


class CancelResult(Contract):
    request_id: Id
    status: LedgerRequestStatus
    cancelled: bool
    needs_reconciliation: bool
    reason: Id


_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_ACCOUNTED = frozenset({"sent", "succeeded", "failed", "outcome_unknown"})


def _clip(value: str, limit: int = 160) -> str:
    return value[:limit]


class RequestLedger:
    """In-memory ledger with optional JSON file persistence.

    A new ``RequestLedger`` instance that points at an existing file reloads every
    historical entry. Entries are only dropped by deleting the file itself.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else None
        self._entries: dict[str, LedgerEntry] = {}
        self._by_key: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------ storage
    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for item in raw.get("entries", []):
            entry = LedgerEntry.model_validate(item)
            self._entries[entry.request_id] = entry
            self._by_key.setdefault(entry.idempotency_key, entry.request_id)

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"entries": [entry.model_dump(mode="json") for entry in self._entries.values()]}
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def _find(self, request_id_or_key: str) -> LedgerEntry:
        entry = self._entries.get(request_id_or_key)
        if entry is None:
            request_id = self._by_key.get(request_id_or_key)
            entry = self._entries.get(request_id) if request_id else None
        if entry is None:
            raise KeyError(request_id_or_key)
        return entry

    def _store(self, entry: LedgerEntry) -> LedgerEntry:
        self._entries[entry.request_id] = entry
        self._by_key.setdefault(entry.idempotency_key, entry.request_id)
        self._persist()
        return entry

    # ------------------------------------------------------------------ protocol
    def prepare(
        self,
        attempt_id: str,
        logical_task_id: str,
        run_id: str,
        task_id: str,
        role_id: str,
        task_kind: str,
        input_hash: str,
        idempotency_key: str,
        generation: int = 1,
        step_index: int = 1,
        kind: RequestKind = "model",
        mode: Mode = "mock",
        request_id: str | None = None,
        is_resend: bool = False,
    ) -> LedgerEntry:
        """Register a request before it is sent, reserving one call of budget."""
        existing_id = self._by_key.get(idempotency_key)
        if existing_id is not None:
            existing = self._entries[existing_id]
            if not is_resend:
                if existing.input_hash != input_hash:
                    raise ValueError("idempotency key conflict")
                return existing
            if existing.status == "outcome_unknown":
                raise RuntimeError(
                    "cannot blindly resend outcome_unknown request before reconciliation"
                )
            new_key = self._next_resend_key(idempotency_key)
            return self._store(
                LedgerEntry(
                    request_id=request_id or self._new_request_id(),
                    idempotency_key=new_key,
                    attempt_id=attempt_id,
                    logical_task_id=logical_task_id,
                    run_id=run_id,
                    task_id=task_id,
                    role_id=role_id,
                    task_kind=task_kind,
                    generation=generation,
                    step_index=step_index,
                    kind=kind,
                    input_hash=input_hash,
                    mode=mode,
                    status="prepared",
                    resend_of_request_id=existing.request_id,
                )
            )
        return self._store(
            LedgerEntry(
                request_id=request_id or self._new_request_id(),
                idempotency_key=idempotency_key,
                attempt_id=attempt_id,
                logical_task_id=logical_task_id,
                run_id=run_id,
                task_id=task_id,
                role_id=role_id,
                task_kind=task_kind,
                generation=generation,
                step_index=step_index,
                kind=kind,
                input_hash=input_hash,
                mode=mode,
                status="prepared",
            )
        )

    def mark_sent(self, request_id: str, provider_request_id: str | None = None) -> LedgerEntry:
        entry = self._find(request_id)
        if entry.status in _TERMINAL or entry.status == "outcome_unknown":
            raise RuntimeError(f"cannot mark {entry.status} request as sent")
        entry.status = "sent"
        if provider_request_id is not None:
            entry.provider_request_id = provider_request_id
        return self._store(entry)

    def record_outcome(
        self,
        request_id: str,
        status: OutcomeStatus,
        *,
        response_summary: dict[str, JsonValue] | None = None,
        error_code: str | None = None,
        current_generation: int | None = None,
    ) -> LedgerEntry:
        if status not in ("succeeded", "failed", "outcome_unknown"):
            raise ValueError(f"invalid outcome status: {status}")
        entry = self._find(request_id)
        if entry.status == "cancelled" or (
            current_generation is not None and current_generation > entry.generation
        ):
            entry.late_response_ignored = True
            entry.late_response_reason = "late_or_stale_generation"
            return self._store(entry)
        entry.status = status
        entry.response_summary = response_summary
        entry.error_code = error_code
        if status == "outcome_unknown":
            # The call may have happened: never release budget or zero the usage.
            entry.calls_consumed = 1
            entry.budget_held = True
            entry.needs_reconciliation = True
        else:
            entry.calls_consumed = 1
            entry.budget_held = False
            entry.needs_reconciliation = False
        return self._store(entry)

    def query(
        self,
        request_id_or_key: str,
        *,
        provider_status_hook: Callable[[LedgerEntry], str | None] | None = None,
    ) -> QueryResult:
        entry = self._find(request_id_or_key)
        if provider_status_hook is not None and entry.status in ("outcome_unknown", "sent"):
            provider_status = provider_status_hook(entry)
            if provider_status in (None, "unknown"):
                entry.status = "outcome_unknown"
                entry.calls_consumed = 1
                entry.budget_held = True
                entry.needs_reconciliation = True
            elif provider_status == "succeeded":
                entry.status = "succeeded"
                entry.calls_consumed = 1
                entry.budget_held = False
                entry.needs_reconciliation = False
            elif provider_status == "failed":
                entry.status = "failed"
                entry.calls_consumed = 1
                entry.budget_held = False
                entry.needs_reconciliation = False
            elif provider_status == "cancelled":
                entry.status = "cancelled"
                entry.calls_consumed = 1
                entry.budget_held = False
                entry.needs_reconciliation = False
            self._store(entry)
        return QueryResult(
            request_id=entry.request_id,
            idempotency_key=entry.idempotency_key,
            attempt_id=entry.attempt_id,
            status=entry.status,
            is_terminal=entry.status in _TERMINAL,
            needs_reconciliation=entry.needs_reconciliation,
            budget_held=entry.budget_held,
            provider_request_id=entry.provider_request_id,
            late_response_ignored=entry.late_response_ignored,
        )

    def cancel(
        self,
        request_id_or_key: str,
        *,
        provider_cancel_hook: Callable[[LedgerEntry], CancelHookStatus] | None = None,
    ) -> CancelResult:
        entry = self._find(request_id_or_key)
        if entry.status in _TERMINAL:
            return CancelResult(
                request_id=entry.request_id,
                status=entry.status,
                cancelled=entry.status == "cancelled",
                needs_reconciliation=entry.needs_reconciliation,
                reason="already_terminal",
            )
        if provider_cancel_hook is None and entry.status in ("sent", "outcome_unknown"):
            # No provider confirmation: never claim a clean cancellation.
            entry.cancel_requested = True
            entry.status = "outcome_unknown"
            entry.calls_consumed = 1
            entry.budget_held = True
            entry.needs_reconciliation = True
            self._store(entry)
            return CancelResult(
                request_id=entry.request_id,
                status=entry.status,
                cancelled=False,
                needs_reconciliation=True,
                reason="provider_unknown",
            )
        hook_status: CancelHookStatus = (
            provider_cancel_hook(entry) if provider_cancel_hook is not None else "cancelled"
        )
        if hook_status == "already_terminal":
            return CancelResult(
                request_id=entry.request_id,
                status=entry.status,
                cancelled=False,
                needs_reconciliation=entry.needs_reconciliation,
                reason="already_terminal",
            )
        entry.cancel_requested = True
        if hook_status == "cancelled":
            was_sent = entry.status == "sent" or entry.provider_request_id is not None
            entry.status = "cancelled"
            entry.needs_reconciliation = False
            entry.budget_held = False
            entry.calls_consumed = 1 if was_sent else 0
            reason = "provider_cancelled" if was_sent else "cancelled_before_send"
        else:
            # Provider state is ambiguous: keep budget reserved and reconcilable.
            entry.status = "outcome_unknown"
            entry.calls_consumed = 1
            entry.budget_held = True
            entry.needs_reconciliation = True
            reason = "provider_unknown"
        self._store(entry)
        return CancelResult(
            request_id=entry.request_id,
            status=entry.status,
            cancelled=entry.status == "cancelled",
            needs_reconciliation=entry.needs_reconciliation,
            reason=reason,
        )

    # ------------------------------------------------------------------ helpers
    def total_calls_accounted(self, run_id: str | None = None) -> int:
        total = 0
        for entry in self._entries.values():
            if run_id is not None and entry.run_id != run_id:
                continue
            if entry.status in _ACCOUNTED or (entry.status == "cancelled" and entry.calls_consumed > 0):
                total += 1
        return total

    def to_attempt(self, request_id: str) -> Attempt:
        entry = self._find(request_id)
        status = entry.status
        attempt_status: Literal["prepared", "sent", "succeeded", "failed", "outcome_unknown"] = (
            "failed" if status == "cancelled" else status
        )
        return Attempt(
            attempt_id=entry.attempt_id,
            logical_task_id=entry.logical_task_id,
            run_id=entry.run_id,
            task_id=entry.task_id,
            generation=entry.generation,
            input_hash=entry.input_hash,
            provider_request_id=entry.provider_request_id,
            idempotency_key=entry.idempotency_key,
            mode=entry.mode,
            status=attempt_status,
        )

    def entries(self, run_id: str | None = None) -> list[LedgerEntry]:
        values = list(self._entries.values())
        if run_id is None:
            return values
        return [entry for entry in values if entry.run_id == run_id]

    def __len__(self) -> int:
        return len(self._entries)

    @staticmethod
    def _new_request_id() -> str:
        return f"REQ-{uuid4().hex[:16]}"

    def _next_resend_key(self, key: str) -> str:
        index = 1
        while True:
            candidate = _clip(f"{key}-resend-{index}")
            if candidate not in self._by_key:
                return candidate
            index += 1


def hash_refs(refs: list[Ref]) -> str:
    """Deterministic input hash for a list of contract references."""
    canonical = sorted(
        (ref.model_dump(mode="json") for ref in refs),
        key=lambda item: (item["object_id"], item["version"], item["content_hash"]),
    )
    return fingerprint(canonical)

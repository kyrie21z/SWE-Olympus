"""Runner, ledger and execution-record behaviour under mock and unconfigured real providers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from olympus.agents.runtime import (
    AgentRunner,
    ExecutionRecordService,
    InputWhitelistError,
    MockProvider,
    OutputSchemaError,
    ProviderNotConfiguredError,
    RealProvider,
    RolePermissions,
    RoleRunInput,
    RunnerBudget,
    RunnerPermissionError,
    UnknownOutcomeError,
    strip_internal_reasoning,
)
from olympus.contracts.models import ExecutionRecord, Ref
from olympus.requests import RequestLedger

ROOT = Path(__file__).resolve().parents[2]
REF = Ref(object_id="DS-001", version=1, content_hash="a" * 64)


def permissions(
    role_id: str = "Daedalus",
    task_kinds: list[str] | None = None,
    input_keys: list[str] | None = None,
    scopes: list[str] | None = None,
) -> RolePermissions:
    return RolePermissions(
        role_id=role_id,
        allowed_task_kinds=task_kinds or ["IMPLEMENT"],
        allowed_input_keys=input_keys or ["design"],
        allowed_write_scopes=scopes if scopes is not None else ["backend/olympus/agents"],
    )


def role_request(
    *,
    role_id: str = "Daedalus",
    task_kind: str = "IMPLEMENT",
    logical_task_id: str = "LT-001",
    task_id: str = "TASK-001",
    manifest: dict | None = None,
    schema: dict | None = None,
    fixture_key: str | None = None,
    idempotency_key: str | None = None,
    generation: int = 1,
    budget_calls: int | None = None,
    refs: list[Ref] | None = None,
    perms: RolePermissions | None = None,
) -> RoleRunInput:
    return RoleRunInput(
        role_id=role_id,
        task_kind=task_kind,
        logical_task_id=logical_task_id,
        task_id=task_id,
        generation=generation,
        input_manifest=manifest if manifest is not None else {"design": "DS-001"},
        input_refs=refs if refs is not None else [REF],
        output_schema=schema or {},
        permissions=perms or permissions(role_id=role_id),
        budget=RunnerBudget(max_calls=budget_calls or 10),
        idempotency_key=idempotency_key,
        fixture_key=fixture_key,
    )


def entry_by_generation(ledger: RequestLedger, generation: int = 1):
    return next(entry for entry in ledger.entries() if entry.generation == generation)


def prepare(ledger: RequestLedger, **overrides):
    payload = {
        "attempt_id": "ATT-001",
        "logical_task_id": "LT-001",
        "run_id": "RUN-001",
        "task_id": "TASK-001",
        "role_id": "Daedalus",
        "task_kind": "IMPLEMENT",
        "input_hash": "c" * 64,
        "idempotency_key": "idem-1",
        "generation": 1,
    }
    payload.update(overrides)
    return ledger.prepare(**payload)


# --------------------------------------------------------------------------- runner
def test_mock_run_is_reproducible_without_api_keys(monkeypatch):
    for key in ("OLYMPUS_MODEL_API_KEY", "OLYMPUS_MODEL_BASE_URL", "OLYMPUS_MODEL_NAME"):
        monkeypatch.delenv(key, raising=False)
    first = AgentRunner(MockProvider()).run(role_request())
    second = AgentRunner(MockProvider()).run(role_request())
    assert first.structured_output == second.structured_output
    assert first.usage == second.usage
    assert first.mode == "mock"
    assert first.attempt.status == "succeeded"
    assert first.attempt.provider_request_id is not None


def test_real_provider_unavailable_when_unconfigured(monkeypatch):
    for key in ("OLYMPUS_MODEL_API_KEY", "OLYMPUS_MODEL_BASE_URL", "OLYMPUS_MODEL_NAME"):
        monkeypatch.delenv(key, raising=False)
    provider = RealProvider()
    assert provider.is_configured() is False
    with pytest.raises(ProviderNotConfiguredError) as error:
        provider.invoke(role_request(), {"kind": "model"}, step_index=1)
    assert "OLYMPUS_MODEL_API_KEY" in str(error.value)
    with pytest.raises(ProviderNotConfiguredError):
        AgentRunner(provider).run(role_request())


def test_input_whitelist_rejects_unlisted_keys():
    with pytest.raises(InputWhitelistError):
        AgentRunner(MockProvider()).run(role_request(manifest={"design": "x", "secret": "y"}))


def test_input_refs_must_not_be_empty():
    with pytest.raises(InputWhitelistError):
        AgentRunner(MockProvider()).run(role_request(refs=[]))


def test_role_cannot_run_unlisted_task_kind():
    with pytest.raises(RunnerPermissionError):
        AgentRunner(MockProvider()).run(role_request(task_kind="REVIEW"))


def test_permissions_must_belong_to_role():
    with pytest.raises(RunnerPermissionError):
        AgentRunner(MockProvider()).run(role_request(perms=permissions(role_id="Momus")))


@pytest.mark.parametrize(
    "extra",
    [
        {"confirmation": {"confirmation_id": "CFM-1"}},
        {"confirmed_by": "YOU"},
        {"call_budget": 99},
        {"calls_used": 0},
        {"state": "accepted"},
        {"verification_status": "passed"},
        {"writes": [{"path": "../secret.py"}]},
        {"writes": [{"path": "/etc/passwd"}]},
        {"files": ["tests/outside.py"]},
    ],
)
def test_privilege_escalation_is_rejected_and_recorded(extra):
    fixtures = {"attack": {"steps": [{"kind": "model", "output": {"summary": "ok"} | extra}]}}
    runner = AgentRunner(MockProvider(fixtures=fixtures))
    with pytest.raises(RunnerPermissionError):
        runner.run(role_request(fixture_key="attack"))
    records = runner.records.list_by_role("Daedalus")
    assert len(records) == 1
    assert records[0].check_outputs
    assert "RunnerPermissionError".upper() in records[0].check_outputs[0].upper()


def test_unauthorized_write_fixture_is_blocked():
    runner = AgentRunner(MockProvider())
    with pytest.raises(RunnerPermissionError):
        runner.run(role_request(fixture_key="unauthorized_write"))


def test_output_schema_and_validator_failures_are_recorded():
    schema = {
        "type": "object",
        "required": ["summary", "files"],
        "properties": {"summary": {"type": "string"}, "files": {"type": "array"}},
    }
    runner = AgentRunner(MockProvider())
    with pytest.raises(OutputSchemaError):
        runner.run(role_request(fixture_key="malformed_output", schema=schema))
    saved = runner.records.list_by_role("Daedalus")
    assert len(saved) == 1
    assert "OutputSchemaError".upper() in saved[0].check_outputs[0].upper()

    with pytest.raises(OutputSchemaError):
        AgentRunner(MockProvider()).run(
            role_request(fixture_key="Daedalus:IMPLEMENT", schema=schema),
            output_validator=lambda output: False,
        )


def test_output_validator_exception_becomes_schema_error():
    def explode(output):
        raise ValueError("bad output")

    with pytest.raises(OutputSchemaError):
        AgentRunner(MockProvider()).run(
            role_request(fixture_key="Daedalus:IMPLEMENT"), output_validator=explode
        )


def test_multi_step_requests_are_all_registered_and_accounted():
    ledger = RequestLedger()
    runner = AgentRunner(MockProvider(), ledger=ledger)
    result = runner.run(role_request(fixture_key="multi_step"))
    assert result.usage.call_count == 3
    entries = ledger.entries()
    assert [entry.step_index for entry in entries] == [1, 2, 3]
    assert [entry.kind for entry in entries] == ["tool", "tool", "model"]
    assert ledger.total_calls_accounted() == 3
    assert ledger.total_calls_accounted("RUN-default") == 3
    assert ledger.total_calls_accounted("RUN-other") == 0
    assert all(entry.status == "succeeded" for entry in entries)
    assert all(entry.budget_held is False for entry in entries)


def test_requests_are_registered_before_the_provider_is_invoked():
    ledger = RequestLedger()

    class SpyProvider(MockProvider):
        def invoke(self, request, step, *, step_index, prompt=None):
            entries = ledger.entries()
            assert len(entries) == step_index
            assert entries[-1].status == "sent"
            assert entries[-1].calls_reserved == 1
            assert entries[-1].budget_held is True
            return super().invoke(request, step, step_index=step_index, prompt=prompt)

    AgentRunner(SpyProvider(), ledger=ledger).run(role_request(fixture_key="multi_step"))
    assert len(ledger) == 3


def test_outcome_unknown_retains_budget_and_blocks_blind_resend():
    ledger = RequestLedger()
    runner = AgentRunner(MockProvider(), ledger=ledger)
    with pytest.raises(UnknownOutcomeError):
        runner.run(role_request(fixture_key="unknown_request"))
    entry = ledger.entries()[0]
    assert entry.status == "outcome_unknown"
    assert entry.calls_consumed == 1
    assert entry.budget_held is True
    assert entry.needs_reconciliation is True
    assert ledger.total_calls_accounted() == 1
    with pytest.raises(RuntimeError, match="cannot blindly resend"):
        ledger.prepare(
            attempt_id=entry.attempt_id,
            logical_task_id=entry.logical_task_id,
            run_id=entry.run_id,
            task_id=entry.task_id,
            role_id=entry.role_id,
            task_kind=entry.task_kind,
            input_hash=entry.input_hash,
            idempotency_key=entry.idempotency_key,
            generation=entry.generation,
            is_resend=True,
        )


def test_late_response_fixture_is_ignored():
    ledger = RequestLedger()
    runner = AgentRunner(MockProvider(), ledger=ledger)
    with pytest.raises(UnknownOutcomeError):
        runner.run(role_request(fixture_key="late_response"))
    entry = ledger.entries()[0]
    assert entry.late_response_ignored is True
    assert entry.late_response_reason == "late_or_stale_generation"
    assert entry.status == "sent"
    assert entry.budget_held is True
    assert runner.records.list_by_role("Daedalus")[0].check_outputs == ["LATE_RESPONSE_IGNORED"]


# --------------------------------------------------------------------------- ledger
def test_prepare_is_idempotent_and_detects_conflict():
    ledger = RequestLedger()
    first = prepare(ledger)
    again = prepare(ledger)
    assert first == again
    assert len(ledger) == 1
    with pytest.raises(ValueError, match="idempotency key conflict"):
        prepare(ledger, input_hash="d" * 64)


def test_resend_creates_new_entry_and_preserves_original():
    ledger = RequestLedger()
    original = prepare(ledger)
    ledger.mark_sent(original.request_id, "provider-1")
    ledger.record_outcome(original.request_id, "succeeded")
    resent = prepare(ledger, is_resend=True)
    assert resent.request_id != original.request_id
    assert resent.idempotency_key != original.idempotency_key
    assert resent.resend_of_request_id == original.request_id
    assert len(ledger) == 2
    assert ledger.query(original.request_id).status == "succeeded"
    assert ledger.query(resent.request_id).status == "prepared"


def test_query_unknown_stays_reconcilable():
    ledger = RequestLedger()
    entry = prepare(ledger)
    ledger.mark_sent(entry.request_id)
    assert ledger.query(entry.request_id, provider_status_hook=lambda item: None).status == "outcome_unknown"
    queried = ledger.query(entry.request_id, provider_status_hook=lambda item: "unknown")
    assert queried.status == "outcome_unknown"
    assert queried.needs_reconciliation is True
    assert queried.budget_held is True
    assert queried.is_terminal is False


def test_query_terminal_and_sent_states():
    ledger = RequestLedger()
    sent = prepare(ledger, idempotency_key="sent-key")
    ledger.mark_sent(sent.request_id)
    snapshot = ledger.query(sent.request_id)
    assert snapshot.status == "sent"
    assert snapshot.is_terminal is False
    assert snapshot.budget_held is True
    ledger.record_outcome(sent.request_id, "succeeded", response_summary={"tokens": 3})
    terminal = ledger.query(sent.request_id, provider_status_hook=lambda item: "failed")
    assert terminal.status == "succeeded"
    assert terminal.is_terminal is True


def test_cancel_unknown_is_not_clean_termination():
    ledger = RequestLedger()
    entry = prepare(ledger)
    ledger.mark_sent(entry.request_id)
    result = ledger.cancel(entry.request_id, provider_cancel_hook=lambda item: "unknown")
    assert result.cancelled is False
    assert result.status == "outcome_unknown"
    assert result.needs_reconciliation is True
    assert result.reason == "provider_unknown"
    stored = entry_by_generation(ledger)
    assert stored.cancel_requested is True
    assert stored.budget_held is True
    assert stored.calls_consumed == 1


def test_cancel_confirmed_and_already_terminal():
    ledger = RequestLedger()
    entry = prepare(ledger)
    ledger.mark_sent(entry.request_id)
    result = ledger.cancel(entry.request_id, provider_cancel_hook=lambda item: "cancelled")
    assert result.cancelled is True
    assert result.status == "cancelled"
    assert result.needs_reconciliation is False
    assert result.reason == "provider_cancelled"
    assert ledger.total_calls_accounted() == 1
    again = ledger.cancel(entry.request_id)
    assert again.cancelled is True
    assert again.reason == "already_terminal"


def test_cancel_before_send_does_not_consume_a_call():
    ledger = RequestLedger()
    entry = prepare(ledger)
    result = ledger.cancel(entry.request_id)
    assert result.cancelled is True
    assert result.reason == "cancelled_before_send"
    assert ledger.entries()[0].calls_consumed == 0
    assert ledger.total_calls_accounted() == 0


def test_late_response_after_cancellation_is_ignored():
    ledger = RequestLedger()
    entry = prepare(ledger)
    ledger.mark_sent(entry.request_id)
    ledger.cancel(entry.request_id, provider_cancel_hook=lambda item: "cancelled")
    late = ledger.record_outcome(entry.request_id, "succeeded", response_summary={"late": True})
    assert late.status == "cancelled"
    assert late.late_response_ignored is True
    assert late.late_response_reason == "late_or_stale_generation"
    assert late.response_summary is None


def test_stale_generation_response_is_ignored():
    ledger = RequestLedger()
    entry = prepare(ledger)
    ledger.mark_sent(entry.request_id)
    stale = ledger.record_outcome(entry.request_id, "succeeded", current_generation=2)
    assert stale.status == "sent"
    assert stale.late_response_ignored is True


def test_outcome_unknown_counts_one_call_never_zero():
    ledger = RequestLedger()
    entry = prepare(ledger)
    ledger.mark_sent(entry.request_id)
    stored = ledger.record_outcome(entry.request_id, "outcome_unknown", error_code="timeout")
    assert stored.calls_consumed == 1
    assert stored.budget_held is True
    assert stored.needs_reconciliation is True
    assert ledger.total_calls_accounted() == 1


def test_to_attempt_projects_valid_contract():
    ledger = RequestLedger()
    entry = prepare(ledger)
    ledger.mark_sent(entry.request_id, "provider-x")
    ledger.record_outcome(entry.request_id, "failed", error_code="boom")
    attempt = ledger.to_attempt(entry.request_id)
    assert attempt.attempt_id == entry.attempt_id
    assert attempt.status == "failed"
    assert attempt.provider_request_id == "provider-x"
    assert attempt.idempotency_key == entry.idempotency_key
    cancelled = prepare(ledger, idempotency_key="cancel-key")
    ledger.cancel(cancelled.request_id)
    assert ledger.to_attempt(cancelled.request_id).status == "failed"


def test_ledger_persists_history_across_instances(tmp_path):
    path = tmp_path / "ledger.json"
    first = RequestLedger(path)
    entry = prepare(first)
    first.mark_sent(entry.request_id, "p-1")
    first.record_outcome(entry.request_id, "succeeded")
    reopened = RequestLedger(path)
    assert len(reopened) == 1
    assert reopened.query(entry.request_id).status == "succeeded"
    assert reopened.total_calls_accounted() == 1
    rebuilt = prepare(reopened)
    assert rebuilt == entry


def test_execution_records_are_queryable_and_reasoning_is_stripped():
    runner = AgentRunner(MockProvider())
    result = runner.run(role_request())
    service = runner.records
    by_attempt = service.get_by_attempt(result.attempt.attempt_id)
    assert by_attempt.record_id == result.execution_record_id
    assert len(service.list_by_role("Daedalus")) == 1
    assert len(service.list_by_role("Daedalus", run_id="RUN-default")) == 1
    assert len(service.list_by_role("Daedalus", run_id="RUN-other")) == 0
    assert len(service.list_by_task("TASK-001")) == 1
    assert len(service.list_by_task("TASK-001", run_id="RUN-default")) == 1
    assert len(service.list_by_logical_task("LT-001")) == 1
    assert "reasoning" not in json.dumps(by_attempt.tools)
    assert "thinking" not in json.dumps(result.structured_output)
    projection = service.project_for_api("RUN-default", role_id="Daedalus", task_id="TASK-001")
    assert len(projection) == 1
    assert projection[0]["role_id"] == "Daedalus"
    assert projection[0]["logical_task_id"] == "LT-001"
    assert projection[0]["record"]["record_id"] == result.execution_record_id
    assert service.project_for_api("RUN-other") == []


def test_execution_record_service_strips_nested_reasoning():
    service = ExecutionRecordService()
    record = ExecutionRecord(
        record_id="ER-X",
        attempt_id="ATT-X",
        task_id="TASK-X",
        inputs=[REF],
        tools=[{"name": "tool", "result": {"thinking": "secret", "value": 1}}],
        artifacts=[],
        diff="",
        check_outputs=[],
        elapsed_ms=1,
        call_count=1,
        retry_history=[],
        mode="mock",
    )
    saved = service.record(record, role_id="Daedalus", logical_task_id="LT-X", run_id="RUN-X")
    assert saved.tools == [{"name": "tool", "result": {"value": 1}}]
    stripped = strip_internal_reasoning({"a": [{"internal_reasoning": "x", "keep": True}]})
    assert stripped == {"a": [{"keep": True}]}


def test_fixture_files_exist_and_parse():
    expected = {
        "mock_responses.json",
        "malformed_output.json",
        "unauthorized_write.json",
        "stale_review.json",
        "unknown_request.json",
        "late_response.json",
    }
    directory = ROOT / "fixtures" / "agents"
    assert expected <= {path.name for path in directory.glob("*.json")}
    for name in expected:
        assert isinstance(json.loads((directory / name).read_text(encoding="utf-8")), dict)

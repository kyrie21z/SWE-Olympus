"""重启核对的无界面场景（P5 · issue #9）。

四个中断点逐一走一遍，并验证一条反向约束：
**核对过程本身不能产生新的请求，也不能盲目重发。**
"""

from __future__ import annotations

import pytest

from olympus.orchestrator.recovery import POINTS, RestartReconciler
from olympus.orchestrator.state_machine import RunContext
from olympus.requests.ledger import RequestLedger

RUN = "RUN-OLYMPUS-001"
INPUT_HASH = "a" * 64


def prepare(ledger: RequestLedger, key: str = "key-1", *, is_resend: bool = False):
    return ledger.prepare(
        attempt_id=f"ATT-{key}",
        logical_task_id="T01",
        run_id=RUN,
        task_id="T01",
        role_id="Hephaestus",
        task_kind="implement",
        input_hash=INPUT_HASH,
        idempotency_key=key,
        is_resend=is_resend,
    )


def advance(ctx: RunContext, task_id: str, target: str) -> None:
    path = ["ready", "running", "verifying", "accepted"]
    for state in path:
        if ctx.state_of(task_id) == state:
            continue
        ctx.transition(
            task_id,
            state,
            reason="场景推进",
            actor="Argus" if state == "accepted" else "Zeus",
            actor_type="agent" if state == "accepted" else "program",
            verification="passed" if state == "accepted" else None,
        )
        if state == target:
            return
        if state == "accepted":
            return


def test_interrupted_before_send_is_cancelled_without_charging():
    ledger = RequestLedger()
    prepare(ledger)

    report = RestartReconciler(ledger).reconcile(RUN)

    item = report.by_point()["before_send"][0]
    assert item.action == "cancelled_before_send"
    stored = ledger.entries()[0]
    assert stored.status == "cancelled"
    assert stored.calls_consumed == 0
    assert stored.budget_held is False
    assert ledger.total_calls_accounted(RUN) == 0


def test_sent_without_provider_query_stays_unknown_and_blocks_resend():
    ledger = RequestLedger()
    entry = prepare(ledger)
    ledger.mark_sent(entry.request_id)

    report = RestartReconciler(ledger).reconcile(RUN)

    item = report.by_point()["sent_unanswered"][0]
    assert item.action == "unverifiable_kept_unknown"
    stored = ledger.entries()[0]
    assert stored.status == "outcome_unknown"
    assert stored.calls_consumed == 1
    assert stored.budget_held is True
    assert stored.needs_reconciliation is True

    # 核对之后仍然不许盲目重发。
    with pytest.raises(RuntimeError):
        prepare(ledger, is_resend=True)


def test_sent_with_provider_confirmation_reaches_terminal_state():
    ledger = RequestLedger()
    entry = prepare(ledger)
    ledger.mark_sent(entry.request_id)

    report = RestartReconciler(ledger, provider_status_hook=lambda _: "succeeded").reconcile(RUN)

    item = report.by_point()["sent_unanswered"][0]
    assert item.action == "queried_terminal"
    stored = ledger.entries()[0]
    assert stored.status == "succeeded"
    assert stored.budget_held is False


def test_sent_with_active_query_that_stays_unknown_keeps_budget():
    ledger = RequestLedger()
    entry = prepare(ledger)
    ledger.mark_sent(entry.request_id)

    report = RestartReconciler(ledger, provider_status_hook=lambda _: None).reconcile(RUN)

    item = report.by_point()["sent_unanswered"][0]
    assert item.action == "queried_unknown"
    assert item.budget_held is True
    assert item.needs_reconciliation is True
    assert ledger.entries()[0].status == "outcome_unknown"


def test_all_four_points_are_reported(bundle):
    ledger = RequestLedger()
    prepare(ledger)

    ctx = RunContext(run=bundle.run.model_copy(deep=True))
    advance(ctx, "T01", "verifying")     # 工件已保存未接纳
    advance(ctx, "T02", "accepted")      # 已接纳

    report = RestartReconciler(ledger).reconcile(RUN, ctx=ctx)
    grouped = report.by_point()

    assert set(grouped) == set(POINTS)
    assert [item.subject_id for item in grouped["before_send"]] == [ledger.entries()[0].request_id]
    assert [item.subject_id for item in grouped["artifact_saved"]] == ["T01"]
    assert [item.subject_id for item in grouped["accepted"]] == ["T02"]
    assert report.summary() == {
        "before_send": 1,
        "sent_unanswered": 0,
        "artifact_saved": 1,
        "accepted": 1,
    }
    assert grouped["artifact_saved"][0].action == "awaiting_acceptance"
    assert grouped["accepted"][0].action == "kept"


def test_reconciliation_never_creates_new_requests(bundle):
    ledger = RequestLedger()
    entry = prepare(ledger)
    ledger.mark_sent(entry.request_id)
    before = len(ledger)

    RestartReconciler(ledger).reconcile(RUN)

    assert len(ledger) == before


def test_empty_run_still_reports_four_points():
    report = RestartReconciler(RequestLedger()).reconcile(RUN)

    assert report.summary() == {
        "before_send": 0,
        "sent_unanswered": 0,
        "artifact_saved": 0,
        "accepted": 0,
    }
    assert report.needs_human() == []

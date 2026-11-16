"""失败归因与有界重试策略的测试。

覆盖 issue #9 的四句硬要求：

1. 首次 + **最多两次**自动重试；
2. 结果未知时**先查询核对**，查询前禁止重发；
3. 自动额度用尽**转人工**；
4. 归因为上游时**创建修复子任务**，不重试本任务。
"""

from __future__ import annotations

from olympus.agents.momus.failure import FailureEvidence, HeuristicFailureAttributor
from olympus.orchestrator.recovery import BoundedRetryPolicy


def attribute(**kwargs):
    return HeuristicFailureAttributor().attribute(FailureEvidence(**kwargs))


def test_scope_violation_blames_the_task():
    result = attribute(task_id="T03", scope_violations=("src/other.py",))

    assert result.category == "task"
    assert result.responsible_task_id == "T03"
    assert result.mode == "heuristic"


def test_missing_upstream_input_blames_the_upstream():
    result = attribute(
        task_id="T06",
        missing_inputs=("A03",),
        upstream_candidates=("T03",),
    )

    assert result.category == "upstream"
    assert result.responsible_task_id == "T03"
    assert "A03" in result.reasons[0]


def test_design_mismatch_goes_back_to_design():
    result = attribute(task_id="T02", design_mismatches=("D-SM-01 未覆盖取消态",))

    assert result.category == "design"


def test_check_failure_is_task_fault():
    result = attribute(task_id="T03", check_failures=("test_waitlist_order 失败",))

    assert result.category == "task"


def test_no_evidence_is_unknown_not_a_guess():
    result = attribute(task_id="T03")

    assert result.category == "unknown"


def test_outcome_unknown_is_not_attributed():
    result = attribute(task_id="T03", outcome="outcome_unknown")

    assert result.category == "unknown"
    assert "查询" in result.reasons[0]


def test_task_failure_retries_exactly_twice_then_human():
    policy = BoundedRetryPolicy()
    failure = attribute(task_id="T03", check_failures=("失败",))

    first = policy.decide(failure)
    second = policy.decide(failure)
    third = policy.decide(failure)

    assert [first.action, second.action, third.action] == ["retry", "retry", "human"]
    assert [first.auto_retry_no, second.auto_retry_no, third.auto_retry_no] == [1, 2, 2]
    assert policy.auto_retries("T03") == 2


def test_upstream_failure_requests_repair_without_spending_task_quota():
    policy = BoundedRetryPolicy()
    upstream = attribute(task_id="T06", missing_inputs=("A03",), upstream_candidates=("T03",))

    decision = policy.decide(upstream)

    assert decision.action == "repair"
    assert decision.auto_retry_no == 0
    # 上游归因不消耗被阻塞任务自己的自动额度。
    assert policy.auto_retries("T06") == 0


def test_unknown_outcome_requires_query_before_any_retry():
    policy = BoundedRetryPolicy()
    unknown = attribute(task_id="T03", outcome="outcome_unknown")

    decision = policy.decide(unknown, outcome_unknown=True)

    assert decision.action == "query"
    assert "禁止盲目重发" in decision.reason


def test_query_that_stays_unknown_goes_human_with_budget_held():
    policy = BoundedRetryPolicy()
    unknown = attribute(task_id="T03", outcome="outcome_unknown")
    policy.note_query("T03")

    decision = policy.decide(unknown, outcome_unknown=True)

    assert decision.action == "human"
    assert "预算保持占用" in decision.reason
    # 查询不是重试，不消耗自动额度。
    assert policy.auto_retries("T03") == 0


def test_design_problem_never_auto_retries():
    policy = BoundedRetryPolicy()
    decision = policy.decide(attribute(task_id="T02", design_mismatches=("漏状态",)))

    assert decision.action == "human"
    assert "改版" in decision.reason

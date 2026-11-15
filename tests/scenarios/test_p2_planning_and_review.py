"""P2 end-to-end: Daedalus plans and Momus reviews through the shared runtime.

The scenario wires the issue #4 planner/reviewer to the issue #3 ``AgentRunner``,
``RequestLedger``, ``ExecutionRecordService`` and ``CommonReviewer``, then hands the
accepted plan to the package compiler and the traceability index. Every model call
is a deterministic mock; no provider credential is involved.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

from olympus.agents.daedalus import DaedalusPlanner
from olympus.agents.momus.planning import MomusPlanReviewer, PlanningWorkflow
from olympus.agents.runtime import AgentRunner, ExecutionRecordService, MockProvider
from olympus.planning.service import PlanService
from olympus.requests import RequestLedger
from olympus.reviews.common import CommonReviewer
from olympus.traceability.planning import build_planning_traceability

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from export_plans import mutate  # noqa: E402

REQUIREMENT_IDS = ["FR-001", "FR-002", "FR-003", "FR-004", "FR-005"]


class P2Runtime(NamedTuple):
    planner: DaedalusPlanner
    reviewer: MomusPlanReviewer
    ledger: RequestLedger
    records: ExecutionRecordService
    common: CommonReviewer


def build_runtime(planner_factory=DaedalusPlanner) -> P2Runtime:
    ledger = RequestLedger()
    records = ExecutionRecordService()
    runner = AgentRunner(MockProvider(), ledger=ledger, records=records)
    common = CommonReviewer()
    return P2Runtime(
        planner=planner_factory(runner=runner),
        reviewer=MomusPlanReviewer(common_reviewer=common, runner=runner),
        ledger=ledger,
        records=records,
        common=common,
    )


class BrokenPlanPlanner(DaedalusPlanner):
    """First plan drops FR-003 test coverage; the inherited revision repairs it."""

    def plan(self, req, ds):
        self.initial = mutate(
            super().plan(req, ds),
            "broken",
            {"T06": {"sources": ["FR-001", "FR-002", "FR-004", "FR-005", "D-SM-01"]}},
            version=1,
        )
        return self.initial


@pytest.mark.scenario
def test_p2_planning_and_review_end_to_end(bundle):
    runtime = build_runtime()
    outcome = PlanningWorkflow(runtime.planner, runtime.reviewer).run(
        bundle.requirement, bundle.design
    )

    assert outcome.status == "passed"
    assert outcome.plan.version == 1
    assert len(outcome.plan.tasks) == 8
    assert sum(len(task.depends_on) for task in outcome.plan.tasks) == 12
    assert outcome.review.verdict == "passed"
    assert outcome.review.reviewer == "Momus"
    assert outcome.review.target == outcome.plan.ref()

    entries = runtime.ledger.entries()
    assert {(entry.role_id, entry.task_kind) for entry in entries} == {
        ("Daedalus", "plan"),
        ("Momus", "plan_review"),
    }
    assert {entry.logical_task_id for entry in entries} == {
        f"LOG-PLAN-{bundle.requirement.object_id}",
        f"LOG-REVIEW-{outcome.plan.object_id}-V{outcome.plan.version}",
    }
    assert all(entry.status == "succeeded" for entry in entries)
    assert all(entry.mode == "mock" for entry in entries)

    for role in ("Daedalus", "Momus"):
        records = runtime.records.list_by_role(role)
        assert len(records) == 1
        assert records[0].attempt_id in {
            entry.attempt_id for entry in entries if entry.role_id == role
        }
        serialized = json.dumps(
            [record.model_dump(mode="json") for record in records], ensure_ascii=False
        )
        assert "reasoning" not in serialized
        assert "thinking" not in serialized

    assert runtime.common.verify_for_acceptance(
        outcome.review.review_id, current_target=outcome.plan.ref()
    ) is True

    package = PlanService().prepare(bundle.requirement, bundle.design, outcome.plan)
    assert package.plan_ref == outcome.plan.ref()
    assert package.manifest.plan_ref == outcome.plan.ref()
    assert package.manifest.plan_hash == outcome.plan.content_hash
    assert package.approved is False
    assert package.execution_started is False

    index = build_planning_traceability(bundle.requirement, bundle.design, outcome.plan)
    assert len(index.rows) == 5
    assert [row.req_id for row in index.rows] == REQUIREMENT_IDS
    assert all(row.stage_status == "planned" for row in index.rows)
    assert all(row.verification_status == "unverified" for row in index.rows)
    assert all(row.accepted is False for row in index.rows)
    assert index.coverage_complete is True
    assert index.plan_ref == outcome.plan.ref()


@pytest.mark.scenario
def test_p2_single_revision_rebinds_the_review_to_the_new_version(bundle):
    runtime = build_runtime(BrokenPlanPlanner)
    outcome = PlanningWorkflow(runtime.planner, runtime.reviewer).run(
        bundle.requirement, bundle.design
    )

    assert outcome.status == "passed"
    assert outcome.revisions_used == 1
    assert len(outcome.reviews) == 2
    assert outcome.plan.version == 2

    first = outcome.reviews[0]
    assert first.verdict == "blocked"
    assert first.target == runtime.planner.initial.ref()
    assert first.target != outcome.plan.ref()
    assert any("MISSING_TEST_COVERAGE" in finding for finding in first.findings)
    assert outcome.review.target == outcome.plan.ref()
    assert outcome.review.verdict == "passed"

    assert runtime.common.verify_for_acceptance(
        first.review_id, current_target=outcome.plan.ref()
    ) is False
    assert runtime.common.verify_for_acceptance(
        outcome.review.review_id, current_target=outcome.plan.ref()
    ) is True
    assert runtime.common.get(first.review_id).review.target == runtime.planner.initial.ref()

    review_entries = [entry for entry in runtime.ledger.entries() if entry.task_kind == "plan_review"]
    assert {entry.logical_task_id for entry in review_entries} == {
        f"LOG-REVIEW-{runtime.planner.initial.object_id}-V1",
        f"LOG-REVIEW-{outcome.plan.object_id}-V2",
    }
    assert all(entry.status == "succeeded" for entry in review_entries)
    assert len(runtime.records.list_by_role("Momus")) == 2

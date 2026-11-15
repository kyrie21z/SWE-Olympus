"""#10 的视图接口测试：运行摘要、追踪矩阵、执行详情。

三条原则：

1. 视图来自 P2/P3/P4 的服务，API 不重新判断；
2. 缺依赖时明确报"不可用"，不拼一份假数据；
3. 计划改版后追踪索引必须跟着更新。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from olympus.app import create_app
from olympus.orchestrator.recovery import plan_repair
from olympus.planning.service import PlanService
from olympus.storage.sqlite import Store


class FakeExecutions:
    def __init__(self) -> None:
        self.queries: list[tuple[str, str | None]] = []

    def project(self, task_id: str, attempt_id: str | None = None) -> dict:
        self.queries.append((task_id, attempt_id))
        if task_id == "T99":
            raise KeyError(task_id)
        return {
            "run_id": "RUN-DEMO",
            "task_id": task_id,
            "attempt_id": attempt_id or "ATT-1",
            "runtime": {"state": "verifying", "verification_status": "unverified"},
            "execution": {"tool_invocations": 2, "diff": "+ lock.acquire()"},
        }


def build(tmp_path: Path, bundle, *, with_docs: bool = True, execution_view=None):
    store = Store(tmp_path / "state.sqlite3")
    store.create(bundle.run)
    app = create_app(store=store, execution_view=execution_view)
    client = TestClient(app)
    runtime = app.state.runs.registry.get(bundle.run.run_id)
    package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
    if with_docs:
        runtime.attach_plan(
            bundle.plan,
            package.manifest,
            requirement=bundle.requirement,
            design=bundle.design,
        )
    else:
        runtime.attach_plan(bundle.plan, package.manifest)
    return client, app, runtime


def test_view_bundles_authoritative_state(tmp_path, bundle):
    client, _, runtime = build(tmp_path, bundle)

    body = client.get(f"/api/runs/{bundle.run.run_id}/view").json()

    assert body["run_id"] == bundle.run.run_id
    assert body["snapshot"]["run_id"] == bundle.run.run_id
    assert body["projection"]["progress"]["required"] == len(bundle.plan.tasks)
    assert {row["action"] for row in body["allowed_actions"]} >= {
        "retry",
        "reassign",
        "accept_risk",
        "request_change",
    }
    assert body["trace"]["plan_ref"]["version"] == bundle.plan.version
    assert body["repairs"] == []


def test_view_requires_plan_binding(tmp_path, bundle):
    client, app, _ = build(tmp_path, bundle)
    runtime = app.state.runs.registry.get(bundle.run.run_id)
    runtime.scheduler = None
    runtime.bus = None
    runtime.handlers = None

    response = client.get(f"/api/runs/{bundle.run.run_id}/view")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PLAN_NOT_BOUND"


def test_trace_requires_documents(tmp_path, bundle):
    client, _, _ = build(tmp_path, bundle, with_docs=False)

    response = client.get(f"/api/runs/{bundle.run.run_id}/trace")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TRACE_UNAVAILABLE"


def test_trace_follows_plan_revision_after_repair(tmp_path, bundle):
    client, _, runtime = build(tmp_path, bundle)
    before = client.get(f"/api/runs/{bundle.run.run_id}/trace").json()

    plan_repair(runtime, "T03", failure_ref="REC-001", reason="重复递补")
    after = client.get(f"/api/runs/{bundle.run.run_id}/trace").json()

    assert after["plan_ref"]["version"] == before["plan_ref"]["version"] + 1
    assert after["task_to_sources"].get("T03-R1")
    assert after["index_hash"] != before["index_hash"]


def test_executions_require_injected_provider(tmp_path, bundle):
    client, _, _ = build(tmp_path, bundle)

    response = client.get(f"/api/runs/{bundle.run.run_id}/executions/T01")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EXECUTION_VIEW_UNAVAILABLE"


def test_executions_delegate_to_provider(tmp_path, bundle):
    provider = FakeExecutions()
    client, _, _ = build(tmp_path, bundle, execution_view=provider)

    body = client.get(
        f"/api/runs/{bundle.run.run_id}/executions/T01", params={"attempt_id": "ATT-9"}
    ).json()

    assert body["task_id"] == "T01"
    assert body["attempt_id"] == "ATT-9"
    assert provider.queries == [("T01", "ATT-9")]


def test_unknown_execution_is_404(tmp_path, bundle):
    client, _, _ = build(tmp_path, bundle, execution_view=FakeExecutions())

    response = client.get(f"/api/runs/{bundle.run.run_id}/executions/T99")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EXECUTION_NOT_FOUND"

"""命令接口测试。

重点不是"能返回 200"，而是四条承诺：

1. 没绑定计划 → 明确拒绝（409），不装作能执行；
2. 陈旧修订号 → 拒绝回执，不覆盖别人的修改；
3. 同一 operation_id 重放 → 返回同一回执，且**不产生新事件**；
4. 每次写操作都如实声明 durability，调用方无需读文档猜。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from olympus.app import create_app
from olympus.contracts.models import Command
from olympus.planning.service import PlanService
from olympus.storage.sqlite import Store


def build(tmp_path: Path, bundle, *, attach_plan: bool = True):
    store = Store(tmp_path / "state.sqlite3")
    store.create(bundle.run)
    app = create_app(store=store)
    client = TestClient(app)
    if attach_plan:
        package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
        app.state.runs.registry.get(bundle.run.run_id).attach_plan(bundle.plan, package.manifest)
    return client, app, store


def command(bundle, action: str, *, operation_id: str, revision: int, target: str | None = None) -> dict:
    payload = Command(
        operation_id=operation_id,
        run_id=bundle.run.run_id,
        actor="YOU",
        action=action,
        target_id=target or bundle.run.run_id,
        expected_version=1,
        expected_state_revision=revision,
        parameters={},
    )
    return payload.model_dump(mode="json")


def test_start_accepts_and_declares_sqlite(tmp_path, bundle):
    client, app, _ = build(tmp_path, bundle)

    response = client.post(
        "/api/commands",
        json=command(bundle, "start", operation_id="op-start", revision=0),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.headers["x-olympus-durability"] == "sqlite"
    assert int(response.headers["x-olympus-recorded-events"]) > 1
    # 读接口与命令接口共用运行态：刚改的状态必须立刻可见。
    assert client.get(f"/api/runs/{bundle.run.run_id}").json()["status"] == "active"


def test_unknown_run_is_404(tmp_path, bundle):
    client, _, _ = build(tmp_path, bundle)
    payload = command(bundle, "start", operation_id="op-x", revision=0)
    payload["run_id"] = "RUN-missing"

    response = client.post("/api/commands", json=payload)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_plan_not_bound_is_explicit(tmp_path, bundle):
    client, _, _ = build(tmp_path, bundle, attach_plan=False)

    response = client.post(
        "/api/commands",
        json=command(bundle, "start", operation_id="op-no-plan", revision=0),
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PLAN_NOT_BOUND"
    assert error["detail"]["run_id"] == bundle.run.run_id


def test_stale_revision_is_rejected_not_overwritten(tmp_path, bundle):
    client, app, _ = build(tmp_path, bundle)
    client.post("/api/commands", json=command(bundle, "start", operation_id="op-start", revision=0))
    runtime = app.state.runs.registry.get(bundle.run.run_id)
    current = runtime.ctx.run.state_revision

    response = client.post(
        "/api/commands",
        json=command(bundle, "pause", operation_id="op-pause-stale", revision=current - 1),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert response.json()["reason_code"] == "stale"
    assert runtime.ctx.run.state_revision == current


def test_replay_is_idempotent_and_adds_no_events(tmp_path, bundle):
    client, app, _ = build(tmp_path, bundle)
    payload = command(bundle, "start", operation_id="op-start", revision=0)

    first = client.post("/api/commands", json=payload)
    runtime = app.state.runs.registry.get(bundle.run.run_id)
    events_after_first = len(runtime.ctx.events)

    second = client.post("/api/commands", json=payload)

    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(runtime.ctx.events) == events_after_first


def test_retry_rejects_task_in_wrong_state(tmp_path, bundle):
    """处理器已注册：不再是 NOT_READY，而是按任务实际状态拒绝。"""
    client, app, _ = build(tmp_path, bundle)
    client.post("/api/commands", json=command(bundle, "start", operation_id="op-start", revision=0))
    runtime = app.state.runs.registry.get(bundle.run.run_id)
    task = next(task for task in bundle.plan.tasks if task.task_id == "T01")

    response = client.post(
        "/api/commands",
        json=command(
            bundle,
            "retry",
            operation_id="op-retry",
            revision=runtime.ctx.run.state_revision,
            target=task.task_id,
        ),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    # T01 此时是 ready，只有 failed/blocked 才能重试。
    assert response.json()["reason_code"] == "bad_state"


def test_capabilities_declare_the_current_limits(tmp_path, bundle):
    client, _, _ = build(tmp_path, bundle)

    body = client.get("/api/capabilities").json()

    assert body["durability"] == "sqlite"
    assert body["command_journal"] == "sqlite"
    assert body["recovery"]["mode"] == "single-process"


def test_http_command_and_bound_plan_survive_application_restart(tmp_path, bundle):
    first, _, store = build(tmp_path, bundle)
    payload = command(bundle, "start", operation_id="OP-http-restart", revision=0)
    receipt = first.post("/api/commands", json=payload)
    before = first.get(f"/api/runs/{bundle.run.run_id}/view").json()

    reopened = TestClient(create_app(store=Store(store.path)))
    view = reopened.get(f"/api/runs/{bundle.run.run_id}/view")
    assert view.status_code == 200
    assert view.json()["snapshot"] == before["snapshot"]
    replay = reopened.post("/api/commands", json=payload)
    assert replay.json() == receipt.json()
    assert replay.headers["x-olympus-durability"] == "sqlite"
    assert replay.headers["x-olympus-recorded-events"] == "0"


def test_http_storage_failure_returns_no_success_and_can_retry(tmp_path, bundle):
    client, app, store = build(tmp_path, bundle)
    payload = command(bundle, "start", operation_id="OP-http-disk-failure", revision=0)
    with store.connection() as db:
        db.execute("CREATE TRIGGER fail_command BEFORE INSERT ON operations "
                   "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    result = client.post("/api/commands", json=payload)
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "COMMAND_PERSISTENCE_UNAVAILABLE"
    assert app.state.runs.registry.get(bundle.run.run_id).ctx.run.status == "draft"
    assert store.events(bundle.run.run_id) == []
    with store.connection() as db:
        db.execute("DROP TRIGGER fail_command")
    assert client.post("/api/commands", json=payload).json()["status"] == "accepted"

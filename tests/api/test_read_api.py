"""只读 API 的边界测试。

覆盖的不是"接口能返回 200"，而是四条协议约定：
1. 快照就是数据库里的权威快照，不加工；
2. 未知运行 → 404 且带机器可读 code；
3. 负游标 → 422，不静默纠正；
4. 游标超过已提交序号 → 409，并回报服务端真实序号。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from olympus.app import create_app
from olympus.contracts.models import Command, Run
from olympus.storage.sqlite import Store


def make_client(tmp_path: Path, bundle) -> tuple[TestClient, Store]:
    store = Store(tmp_path / "state.sqlite3")
    store.create(bundle.run)
    return TestClient(create_app(store=store)), store


def pause_command(bundle) -> Command:
    return Command(
        operation_id="op-p5-api-1",
        run_id=bundle.run.run_id,
        actor="YOU",
        action="pause",
        target_id=bundle.run.run_id,
        expected_version=1,
        expected_state_revision=bundle.run.state_revision,
        parameters={},
    )


def paused(run: Run) -> Run:
    proposed = run.model_copy(deep=True)
    proposed.status = "paused"
    return proposed


def test_snapshot_is_authoritative(tmp_path, bundle):
    client, _ = make_client(tmp_path, bundle)

    response = client.get(f"/api/runs/{bundle.run.run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == bundle.run.run_id
    assert body["event_seq"] == 0
    assert body["state_revision"] == 0
    assert body["status"] == "draft"
    assert body["tasks"] == bundle.run.model_dump(mode="json")["tasks"]


def test_unknown_run_uses_error_envelope(tmp_path, bundle):
    client, _ = make_client(tmp_path, bundle)

    response = client.get("/api/runs/RUN-missing")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "RUN_NOT_FOUND"
    assert "RUN-missing" in error["message"]


def test_negative_cursor_is_rejected(tmp_path, bundle):
    client, _ = make_client(tmp_path, bundle)

    response = client.get(f"/api/runs/{bundle.run.run_id}/events", params={"after_seq": -1})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_CURSOR"


def test_cursor_ahead_reports_latest_seq(tmp_path, bundle):
    client, _ = make_client(tmp_path, bundle)

    response = client.get(f"/api/runs/{bundle.run.run_id}/events", params={"after_seq": 5})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "CURSOR_AHEAD"
    assert error["detail"] == {"after_seq": 5, "latest_seq": 0}


def test_events_are_caught_up_by_cursor(tmp_path, bundle):
    client, store = make_client(tmp_path, bundle)
    receipt = store.commit(pause_command(bundle), paused(bundle.run), reason="测试暂停")
    assert receipt.status == "accepted"

    first = client.get(f"/api/runs/{bundle.run.run_id}/events", params={"after_seq": 0})
    assert first.status_code == 200
    events = first.json()
    assert [event["event_seq"] for event in events] == [1]
    # 事件自带同事务快照，客户端不需要"再取一次最新状态"来拼图。
    assert events[0]["snapshot"]["status"] == "paused"
    assert events[0]["snapshot"]["event_seq"] == 1

    already_caught_up = client.get(
        f"/api/runs/{bundle.run.run_id}/events", params={"after_seq": 1}
    )
    assert already_caught_up.json() == []


def test_non_integer_cursor_uses_same_envelope(tmp_path, bundle):
    client, _ = make_client(tmp_path, bundle)

    response = client.get(f"/api/runs/{bundle.run.run_id}/events", params={"after_seq": "abc"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_health_still_answers(tmp_path):
    client = TestClient(create_app(store=Store(tmp_path / "state.sqlite3")))

    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["model_required"] is False

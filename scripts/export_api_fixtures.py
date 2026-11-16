"""录制确定性的 HTTP 请求/响应样例，供 P6 回放。

用法：

    python scripts/export_api_fixtures.py          # 写入 fixtures/http/
    python scripts/export_api_fixtures.py --check  # 比对，漂移返回非零

录制时会抹掉不稳定的字段（事件 ID、时间戳），其余内容逐字节稳定：
同一份夹具 + 同一段命令序列，重复运行结果一致。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from olympus.app import create_app  # noqa: E402
from olympus.contracts.models import Command, ContractBundle  # noqa: E402
from olympus.planning.service import PlanService  # noqa: E402
from olympus.storage.sqlite import Store  # noqa: E402

FIXTURE_DIR = ROOT / "fixtures" / "http"
VOLATILE = frozenset({"event_id", "occurred_at"})


def scrub(value):
    """抹掉每次运行都会变化的字段，让录制结果可比较。"""
    if isinstance(value, dict):
        return {key: ("<volatile>" if key in VOLATILE else scrub(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def load_bundle() -> ContractBundle:
    return ContractBundle.model_validate_json(
        (ROOT / "fixtures" / "contracts" / "valid.json").read_text(encoding="utf-8")
    )


def response_of(response) -> dict:
    return {
        "status": response.status_code,
        "headers": {
            key: response.headers[key]
            for key in ("x-olympus-durability", "x-olympus-recorded-events")
            if key in response.headers
        },
        "body": response.json(),
    }


def record_state() -> dict:
    """只读场景：运行已有一条已提交事件，用于快照与补取样例。"""
    bundle = load_bundle()
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "state.sqlite3")
        store.create(bundle.run)
        proposed = bundle.run.model_copy(deep=True)
        proposed.status = "paused"
        store.commit(
            Command(
                operation_id="op-fixture-pause",
                run_id=bundle.run.run_id,
                actor="YOU",
                action="pause",
                target_id=bundle.run.run_id,
                expected_version=1,
                expected_state_revision=0,
                parameters={},
            ),
            proposed,
            reason="录制样例：已提交的暂停",
        )

        app = create_app(store=store)
        client = TestClient(app)
        package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
        app.state.runs.registry.get(bundle.run.run_id).attach_plan(
            bundle.plan,
            package.manifest,
            requirement=bundle.requirement,
            design=bundle.design,
        )
        run_id = bundle.run.run_id
        return {
            "capabilities": response_of(client.get("/api/capabilities")),
            "snapshot": response_of(client.get(f"/api/runs/{run_id}")),
            "events": response_of(client.get(f"/api/runs/{run_id}/events", params={"after_seq": 0})),
            "trace": response_of(client.get(f"/api/runs/{run_id}/trace")),
            "view": response_of(client.get(f"/api/runs/{run_id}/view")),
        }


def record_commands() -> dict:
    """命令场景：同一运行上的一次成功与一次陈旧回执。"""
    bundle = load_bundle()
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "state.sqlite3")
        store.create(bundle.run)
        app = create_app(store=store)
        client = TestClient(app)
        package = PlanService().prepare(bundle.requirement, bundle.design, bundle.plan)
        app.state.runs.registry.get(bundle.run.run_id).attach_plan(
            bundle.plan,
            package.manifest,
            requirement=bundle.requirement,
            design=bundle.design,
        )
        run_id = bundle.run.run_id
        start = {
            "operation_id": "op-fixture-start",
            "run_id": run_id,
            "actor": "YOU",
            "action": "start",
            "target_id": run_id,
            "expected_version": 1,
            "expected_state_revision": 0,
            "parameters": {},
        }
        accepted = client.post("/api/commands", json=start)

        stale = dict(start, operation_id="op-fixture-stale", action="pause", expected_state_revision=0)
        rejected = client.post("/api/commands", json=stale)

        return {
            "command_start": {"request": start, **response_of(accepted)},
            "command_stale": {"request": stale, **response_of(rejected)},
            "view_after_start": response_of(client.get(f"/api/runs/{run_id}/view")),
        }


def record() -> dict[str, dict]:
    files = {**record_state(), **record_commands()}
    return {name: scrub(payload) for name, payload in files.items()}


def main() -> int:
    check = "--check" in sys.argv
    recorded = record()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    drift: list[str] = []
    for name, payload in sorted(recorded.items()):
        path = FIXTURE_DIR / f"{name}.json"
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                drift.append(name)
        else:
            path.write_text(text, encoding="utf-8")
            print("wrote", path.relative_to(ROOT))
    if check:
        if drift:
            print("drift:", ", ".join(drift))
            return 1
        print("fixtures up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

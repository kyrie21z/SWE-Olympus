"""Real HTTP model transport and actual Python tools across all eight workflow tasks.

The upstream model is a local scripted HTTP server. No provider, runner, execution
adapter or application route is mocked; this verifies integration, not model quality.
"""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from fastapi.testclient import TestClient

from olympus.app import create_app
from olympus.storage.sqlite import Store

ROOT = Path(__file__).resolve().parents[2]
BUSY_STAGES = {"design_generating", "plan_generating", "executing"}


def execution_materials():
    campus = (ROOT / "fixtures/execution/campus_service.py").read_text(encoding="utf-8")
    campus_checks = (ROOT / "fixtures/execution/check_campus.py").read_text(encoding="utf-8")
    changes = {
        "T01": [{"path": "packages/t01/contract.py", "content":
                 'def states():\n    return ("CONFIRMED", "WAITLISTED", "CANCELLED")\n'}],
        "T02": [{"path": "packages/t02/campus.py", "content": campus}],
        "T03": [{"path": "packages/t03/campus.py", "content": campus}],
        "T04": [{"path": "packages/t04/recovery.py", "content":
                 'def recovery_message(state):\n'
                 '    return "重新报名" if state == "CANCELLED" else "查看当前报名"\n'}],
        "T05": [{"path": "packages/t05/view.py", "content":
                 'def query_view(state):\n'
                 '    return {"state": state, "found": state is not None}\n'}],
    }
    checks = {
        "T01": 'from packages.t01.contract import states\n'
               'assert set(states()) == {"CONFIRMED", "WAITLISTED", "CANCELLED"}\n',
        "T02": 'from packages.t02.campus import new_activity, join, cancel, query\n'
               'activity = new_activity(1)\n'
               'assert join(activity, "a") == "CONFIRMED"\n'
               'assert join(activity, "a") == "CONFIRMED"\n'
               'assert len(activity["records"]) == 1\n'
               'assert cancel(activity, "a") == "CANCELLED"\n'
               'assert query(activity, "a") == "CANCELLED"\n',
        "T03": campus_checks,
        "T04": 'from packages.t04.recovery import recovery_message\n'
               'assert recovery_message("CANCELLED") == "重新报名"\n'
               'assert recovery_message("CONFIRMED") == "查看当前报名"\n',
        "T05": 'from packages.t03.campus import new_activity, join, query\n'
               'from packages.t05.view import query_view\n'
               'activity = new_activity(1)\n'
               'assert query_view(query(activity, "a")) == {"state": None, "found": False}\n'
               'join(activity, "a")\n'
               'assert query_view(query(activity, "a")) == {"state": "CONFIRMED", "found": True}\n',
        "T06": campus_checks,
        "T07": 'from packages.t02.campus import new_activity, join, cancel, query\n'
               'from packages.t04.recovery import recovery_message\n'
               'from packages.t05.view import query_view\n'
               'activity = new_activity(1)\n'
               'join(activity, "a")\n'
               'assert query_view(query(activity, "a"))["found"] is True\n'
               'cancel(activity, "a")\n'
               'assert recovery_message(query(activity, "a")) == "重新报名"\n',
        "T08": campus_checks,
    }
    return changes, checks


@contextmanager
def workflow_model_server(bundle, *, failure_status=None, block=None):
    changes, checks = execution_materials()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            role = body["model"].removeprefix("local-")
            user_content = body["messages"][-1]["content"]
            try:
                manifest = json.loads(user_content)
            except json.JSONDecodeError:
                manifest = {"connection_test": True, "prompt": user_content}
            calls.append({"role": role, "manifest": manifest, "path": self.path})
            if block is not None:
                block["marker"].write_text("received", encoding="utf-8")
                block["release"].wait(timeout=30)
            if failure_status is not None:
                self.send_response(failure_status)
                self.end_headers()
                self.wfile.write(b'{"error":"upstream unavailable"}')
                return
            if manifest.get("connection_test"):
                output = {"ok": True}
            elif role == "Athena":
                output = {"design": bundle.design.content}
            elif role == "Daedalus":
                output = {"tasks": [task.semantic_content() for task in bundle.plan.tasks]}
            elif role == "Momus" and "tasks" in manifest and "plan" not in manifest:
                output = {"tasks": [{
                    "task_id": task["task_id"],
                    "checks": {check_id: checks[task["task_id"]] for check_id in task["checks"]},
                } for task in manifest["tasks"]]}
            elif role == "Hephaestus":
                output = {"changes": changes[manifest["task"]["task_id"]]}
            else:
                output = {"verdict": "passed", "findings": []}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(json.dumps({
                    "id": f"scripted-http-{len(calls)}",
                    "choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                }, ensure_ascii=False).encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                # The restart test deliberately terminates the requesting process.
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        if block is not None:
            block["release"].set()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def wait_for_stage(client, workflow_id, expected, *, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/workflows/{workflow_id}")
        assert response.status_code == 200, response.text
        state = response.json()
        if state["stage"] not in BUSY_STAGES:
            assert state["stage"] == expected, json.dumps(state, ensure_ascii=False)
            return state
        time.sleep(0.02)
    raise AssertionError(f"workflow did not reach {expected}")


def action(client, workflow_id, name, expected):
    response = client.post(f"/api/workflows/{workflow_id}/actions", json={
        "action": name, "operation_id": f"{workflow_id}-{name}",
    })
    assert response.status_code == 200, response.text
    return wait_for_stage(client, workflow_id, expected)


def create(client, requirement, *, allow_synthetic=True):
    response = client.post("/api/workflows", json={
        "requirement": requirement.model_dump(mode="json"), "allow_synthetic": allow_synthetic,
    })
    assert response.status_code == 200, response.text
    return response.json()


def configure_roles(client, url, *roles):
    for role in roles:
        configured = client.put(f"/api/settings/agents/{role}", json={
            "base_url": url, "model": f"local-{role}", "api_key": f"local-secret-{role}",
        })
        assert configured.status_code == 200, configured.text


def test_all_eight_tasks_use_real_model_http_and_tools_and_survive_app_restart(tmp_path, bundle):
    database = tmp_path / "state.sqlite3"
    with workflow_model_server(bundle) as (url, calls):
        with TestClient(create_app(store=Store(database))) as client:
            for role in ("Athena", "Daedalus", "Momus", "Hephaestus", "Argus", "Hermes"):
                configured = client.put(f"/api/settings/agents/{role}", json={
                    "base_url": url, "model": f"local-{role}", "api_key": f"local-secret-{role}",
                })
                assert configured.status_code == 200, configured.text
                assert "local-secret" not in configured.text
            initial = create(client, bundle.requirement)
            workflow_id, run_id = initial["workflow_id"], initial["run_id"]
            action(client, workflow_id, "approve_requirement", "requirement_approved")
            designed = action(client, workflow_id, "generate_design", "design_pending_approval")
            assert designed["design"]["requirement"]["object_id"] == bundle.requirement.object_id
            action(client, workflow_id, "approve_design", "design_approved")
            planned = action(client, workflow_id, "generate_plan", "plan_pending_approval")
            assert len(planned["plan"]["tasks"]) == len(planned["acceptance"]) == 8
            action(client, workflow_id, "approve_plan", "plan_approved")
            executed = action(client, workflow_id, "execute", "awaiting_acceptance")
            assert len(executed["executions"]) == 8
            assert all(task["state"] == "accepted" for task in executed["snapshot"]["tasks"])
            assert all(task["verification_status"] == "passed" for task in executed["snapshot"]["tasks"])
            assert all(item["execution"]["tool_invocations"] > 0 for item in executed["executions"])
            assert all(item["execution"]["review"] is not None for item in executed["executions"])
            completed = action(client, workflow_id, "accept_run", "completed")
            assert completed["snapshot"]["status"] == "completed"
            view = client.get(f"/api/runs/{run_id}/view")
            assert view.status_code == 200, view.text
            assert view.json()["projection"]["progress"] == {"accepted": 8, "required": 8}
            evidence = client.get(f"/api/runs/{run_id}/executions/T03")
            assert evidence.status_code == 200, evidence.text
            assert evidence.json()["execution"]["model_mode"] == "real_model"
            assert evidence.json()["execution"]["checks_passed"] == ["check:T03"]
            assert all(check["exit_code"] == 0 for check in evidence.json()["execution"]["check_outputs"])
            assert "local-secret" not in view.text + evidence.text + json.dumps(completed)
            assert {call["role"] for call in calls} == {"Athena", "Daedalus", "Momus", "Hephaestus", "Argus"}
            assert len([call for call in calls if call["role"] == "Hephaestus"]) == 5
            assert len([call for call in calls if call["role"] == "Argus"]) == 8
            assert all(call["path"] == "/v1/chat/completions" for call in calls)
            calls_before_restart = len(calls)
            snapshot_before_restart = completed["snapshot"]

        with TestClient(create_app(store=Store(database))) as restarted:
            restored = restarted.get(f"/api/workflows/{workflow_id}")
            assert restored.status_code == 200, restored.text
            assert restored.json()["stage"] == "completed"
            assert restored.json()["snapshot"] == snapshot_before_restart
            assert restarted.get(f"/api/runs/{run_id}/view").json()["projection"]["progress"] == {
                "accepted": 8, "required": 8,
            }
            restored_evidence = restarted.get(f"/api/runs/{run_id}/executions/T03")
            assert restored_evidence.status_code == 200, restored_evidence.text
            assert restored_evidence.json()["execution"]["checks_passed"] == ["check:T03"]
            replay = restarted.post(f"/api/workflows/{workflow_id}/actions", json={
                "action": "execute", "operation_id": f"{workflow_id}-execute",
            })
            assert replay.status_code == 200
            assert replay.json()["stage"] == "completed"
            assert len(calls) == calls_before_restart
            capabilities = restarted.get("/api/capabilities").json()
            assert capabilities["durability"] == "sqlite"


def test_workflow_requires_gates_and_reports_missing_roles_in_online_mode(tmp_path, bundle):
    with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"))) as client:
        declined = client.post("/api/workflows", json={
            "requirement": bundle.requirement.model_dump(mode="json"), "allow_synthetic": False,
        })
        assert declined.status_code == 409
        configure_roles(client, "http://127.0.0.1:1/v1", "Momus")
        initial = create(client, bundle.requirement)
        assert initial["model_mode"] == "online"
        workflow_id = initial["workflow_id"]
        premature = client.post(f"/api/workflows/{workflow_id}/actions", json={
            "action": "approve_plan", "operation_id": "premature-plan",
        })
        assert premature.status_code == 409
        untouched = client.get(f"/api/workflows/{workflow_id}").json()
        assert untouched["stage"] == "requirement_pending"
        assert untouched["design"] is None
        action(client, workflow_id, "approve_requirement", "requirement_approved")
        failed = action(client, workflow_id, "generate_design", "error")
        assert failed["design"] is None
        assert failed["error"]["code"] == "AGENT_NOT_CONFIGURED"
        assert failed["snapshot"]["tasks"] == []
        assert failed["retry_safe"] is True
        assert failed["retry_action"] == "generate_design"
        with workflow_model_server(bundle) as (url, calls):
            configure_roles(client, url, "Athena", "Momus")
            retried = client.post(f"/api/workflows/{workflow_id}/actions", json={
                "action": "generate_design", "operation_id": "configured-design-retry",
            })
            assert retried.status_code == 200, retried.text
            designed = wait_for_stage(client, workflow_id, "design_pending_approval")
            assert designed["error"] is None
            assert len(calls) == 2


def test_human_gates_and_changed_operation_are_rejected_without_model_calls(tmp_path, bundle):
    with workflow_model_server(bundle) as (url, calls):
        with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"))) as client:
            configure_roles(client, url, "Athena", "Momus")
            initial = create(client, bundle.requirement)
            workflow_id = initial["workflow_id"]
            for premature in ("approve_design", "generate_plan", "approve_plan", "execute", "accept_run"):
                response = client.post(f"/api/workflows/{workflow_id}/actions", json={
                    "action": premature, "operation_id": f"premature-{premature}",
                })
                assert response.status_code == 409, response.text
            assert calls == []
            action(client, workflow_id, "approve_requirement", "requirement_approved")
            conflicting = client.post(f"/api/workflows/{workflow_id}/actions", json={
                "action": "generate_design", "operation_id": f"{workflow_id}-approve_requirement",
            })
            assert conflicting.status_code == 409
            assert calls == []
            action(client, workflow_id, "generate_design", "design_pending_approval")
            before = len(calls)
            for premature in ("generate_plan", "approve_plan", "execute", "accept_run"):
                response = client.post(f"/api/workflows/{workflow_id}/actions", json={
                    "action": premature, "operation_id": f"unapproved-design-{premature}",
                })
                assert response.status_code == 409
            replay = client.post(f"/api/workflows/{workflow_id}/actions", json={
                "action": "generate_design", "operation_id": f"{workflow_id}-generate_design",
            })
            assert replay.status_code == 200
            assert len(calls) == before
            assert replay.json()["stage"] == "design_pending_approval"


def test_unknown_provider_outcome_holds_budget_and_forbids_blind_retry(tmp_path, bundle):
    with workflow_model_server(bundle, failure_status=503) as (url, calls):
        with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"))) as client:
            configure_roles(client, url, "Athena")
            initial = create(client, bundle.requirement)
            workflow_id = initial["workflow_id"]
            action(client, workflow_id, "approve_requirement", "requirement_approved")
            failed = action(client, workflow_id, "generate_design", "error")
            assert failed["error"]["code"] == "OUTCOME_UNKNOWN"
            assert failed["retry_safe"] is False
            assert failed["allowed_actions"] == []
            assert failed["snapshot"]["calls_used"] == 1
            forbidden = client.post(f"/api/workflows/{workflow_id}/actions", json={
                "action": "generate_design", "operation_id": "unsafe-new-operation",
            })
            assert forbidden.status_code == 409
            replay = client.post(f"/api/workflows/{workflow_id}/actions", json={
                "action": "generate_design", "operation_id": f"{workflow_id}-generate_design",
            })
            assert replay.status_code == 200
            assert replay.json()["error"]["code"] == "OUTCOME_UNKNOWN"
            assert len(calls) == 1
            requests_file = tmp_path / "workflows" / initial["run_id"] / "model-requests.json"
            saved = json.loads(requests_file.read_text(encoding="utf-8"))
            entries = saved["entries"] if isinstance(saved, dict) else saved
            if isinstance(entries, dict):
                entries = list(entries.values())
            assert entries[0]["status"] == "outcome_unknown"
            assert entries[0]["budget_held"] is True


def test_process_restart_preserves_inflight_unknown_and_never_resends(tmp_path, bundle):
    database = tmp_path / "state.sqlite3"
    marker = tmp_path / "upstream-received.txt"
    child_result = tmp_path / "child-workflow.json"
    requirement_file = tmp_path / "requirement.json"
    requirement_file.write_text(bundle.requirement.model_dump_json(), encoding="utf-8")
    helper = tmp_path / "start_then_crash.py"
    helper.write_text('''
import json
import os
from pathlib import Path
import sys
import time
from fastapi.testclient import TestClient
from olympus.app import create_app
from olympus.storage.sqlite import Store

database, base_url, requirement_file, marker, output = sys.argv[1:]
with TestClient(create_app(store=Store(database))) as client:
    configured = client.put('/api/settings/agents/Athena', json={
        'base_url': base_url, 'model': 'local-Athena', 'api_key': 'child-only-key'})
    assert configured.status_code == 200
    created = client.post('/api/workflows', json={
        'requirement': json.loads(Path(requirement_file).read_text(encoding='utf-8')),
        'allow_synthetic': True})
    assert created.status_code == 200, created.text
    doc = created.json()
    workflow_id = doc['workflow_id']
    approved = client.post('/api/workflows/' + workflow_id + '/actions', json={
        'action': 'approve_requirement', 'operation_id': 'child-approve'})
    assert approved.status_code == 200
    generated = client.post('/api/workflows/' + workflow_id + '/actions', json={
        'action': 'generate_design', 'operation_id': 'child-design'})
    assert generated.status_code == 200
    deadline = time.monotonic() + 15
    while not Path(marker).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert Path(marker).exists(), 'upstream never received request'
    Path(output).write_text(json.dumps(doc), encoding='utf-8')
    os._exit(0)
''', encoding="utf-8")
    block = {"marker": marker, "release": threading.Event()}
    with workflow_model_server(bundle, block=block) as (url, calls):
        environment = os.environ.copy()
        environment.update(PYTHONPATH=str(ROOT / "backend"), LANGSMITH_TRACING="false")
        child = subprocess.run(
            [sys.executable, str(helper), str(database), url, str(requirement_file), str(marker), str(child_result)],
            env=environment, capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        block["release"].set()
        assert child.returncode == 0, child.stdout + child.stderr
        created = json.loads(child_result.read_text(encoding="utf-8"))
        workflow_id = created["workflow_id"]
        assert len(calls) == 1
        with TestClient(create_app(store=Store(database))) as restarted:
            restored = restarted.get(f"/api/workflows/{workflow_id}")
            assert restored.status_code == 200, restored.text
            state = restored.json()
            assert state["stage"] == "error"
            assert state["error"]["code"] == "INTERRUPTED_REQUIRES_RECONCILIATION"
            assert state["retry_safe"] is False
            assert state["allowed_actions"] == []
            denied = restarted.post(f"/api/workflows/{workflow_id}/actions", json={
                "action": "generate_design", "operation_id": "restart-new-operation",
            })
            assert denied.status_code == 409
            replay = restarted.post(f"/api/workflows/{workflow_id}/actions", json={
                "action": "generate_design", "operation_id": "child-design",
            })
            assert replay.status_code == 200
            assert replay.json()["retry_safe"] is False
            assert len(calls) == 1

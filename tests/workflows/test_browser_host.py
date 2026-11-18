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
import pytest

from olympus.app import create_app
from olympus.contracts.models import Requirement
from olympus.storage.sqlite import Store
from olympus.workflows.offline import INVENTORY, inventory_profile

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
def workflow_model_server(bundle, *, failure_status=None, block=None, transform=None):
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
            if transform is not None:
                output = transform(role, manifest, output)
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


def action(client, workflow_id, name, expected, *, operation_id=None):
    response = client.post(f"/api/workflows/{workflow_id}/actions", json={
        "action": name, "operation_id": operation_id or f"{workflow_id}-{name}",
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
    argus_returns_invalid_format = True
    revised_goal = "明确生成报名状态接口约定"

    def imperfect_output(role, manifest, output):
        if role == "Athena" and not manifest.get("format_correction"):
            return {"design": {**output["design"], "concurrency": ["expected a string"]}}
        if role == "Momus" and "blockers" in manifest:
            finding, = manifest["blockers"]
            return {"decisions": [{"finding_id": finding["finding_id"], "decision": "resolved", "citations": [
                {"task_id": "T01", "field": "goal", "quote": revised_goal},
            ]}]}
        if role == "Momus" and "plan" in manifest:
            version = manifest["plan"]["version"]
            return {"verdict": "blocked", "findings": [{
                "code": "UNCLEAR_TASK" if version == 1 else "REPEATED_TASK_OBJECTION",
                "severity": "blocker", "task_id": "T01", "message": "Make T01 more explicit.",
            }, {
                "code": "FORMAT_SUGGESTION", "severity": "important", "task_id": "T01", "message": "List examples.",
            }]}
        if role == "Daedalus" and manifest.get("previous_tasks"):
            assert len(manifest["review_findings"]) == 1
            tasks = output["tasks"]
            return {"tasks": [{**tasks[0], "goal": revised_goal}, *tasks[1:]]}
        if role == "Momus" and "tasks" in output and not manifest.get("format_correction"):
            return {"tasks": []}
        if role == "Argus" and argus_returns_invalid_format:
            return {"verdict": "passed", "findings": "invalid format"}
        return output

    database = tmp_path / "state.sqlite3"
    with workflow_model_server(bundle, transform=imperfect_output) as (url, calls):
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
            assert planned["plan"]["version"] == 2
            assert planned["plan"]["tasks"][0]["version"] == 2
            assert planned["plan_review_record"]["blocking_finding_ids"] == []
            assert [f["status"] for f in planned["plan_review_record"]["submission"]["findings"]] == ["closed", "open"]
            assert sum("plan" in call["manifest"] and "blockers" not in call["manifest"] for call in calls) == 1
            assert "concurrency" in [call for call in calls if call["role"] == "Athena"][1]["manifest"]["format_correction"]
            assert [call for call in calls if call["role"] == "Daedalus"][1]["manifest"]["review_findings"]
            action(client, workflow_id, "approve_plan", "plan_approved")
            blocked = action(client, workflow_id, "execute", "execution_blocked")
            first_attempt = blocked["executions"][0]["execution"]["attempt_id"]
            assert blocked["snapshot"]["tasks"][0]["state"] == "verifying"
            argus_returns_invalid_format = False
            assert client.post(f"/api/workflows/{workflow_id}/actions", json={
                "action": "execute", "operation_id": "resume-argus-review",
            }).status_code == 200
            executed = wait_for_stage(client, workflow_id, "awaiting_acceptance")
            first_result = next(row for row in executed["executions"] if row["task_id"] == "T01")
            assert first_result["execution"]["attempt_id"] == first_attempt
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
            assert len([call for call in calls if call["role"] == "Argus"]) == 10
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


def test_design_schema_error_identifies_step_and_keeps_approved_requirement(tmp_path, bundle):
    def wrong_type(role, manifest, output):
        if role == "Athena":
            return {"design": {**output["design"], "concurrency": {"private": "hidden-provider-body"}}}
        return output

    with workflow_model_server(bundle, transform=wrong_type) as (url, calls):
        with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"))) as client:
            configure_roles(client, url, "Athena", "Momus")
            workflow_id = create(client, bundle.requirement)["workflow_id"]
            action(client, workflow_id, "approve_requirement", "requirement_approved")
            failed = action(client, workflow_id, "generate_design", "error")
            assert failed["error"]["code"] == "OUTPUT_SCHEMA_ERROR"
            assert failed["error"]["action"] == "generate_design"
            assert "Athena" in failed["error"]["message"] and "concurrency" in failed["error"]["message"]
            assert "hidden-provider-body" not in json.dumps(failed)
            assert "requirement" in failed["approvals"]
            assert failed["allowed_actions"] == ["generate_design"]
            assert len(calls) == failed["snapshot"]["calls_used"] == 2


def test_design_recheck_keeps_real_issues_and_requires_verbatim_evidence(tmp_path, bundle):
    element = bundle.design.content["elements"][0]
    source = element["sources"][0]
    quote = "同一账号重复提交只保留原报名记录，返回原状态。"
    failure_quote = "报名已取消时重复提交，返回原取消记录，不生成新报名。"
    corrected = False

    def respond(role, manifest, output):
        nonlocal corrected
        if role == "Athena" and manifest.get("previous_design"):
            assert [finding["message"] for finding in manifest["findings"]] == [f"{source}: missing failure case."]
            corrected = True
        if role == "Athena":
            design = output["design"]
            elements = [{**element, "acceptance_method": quote + (failure_quote if corrected else "")},
                        *design["elements"][1:]]
            return {"design": {**design, "elements": elements}}
        if role == "Momus" and "design_content" in manifest and "plan" not in manifest and not corrected:
            return {"verdict": "blocked", "findings": [
                {"code": "FALSE_OMISSION", "severity": "blocker", "message": f"{source}: existing behavior omitted."},
                {"code": "MISSING_FAILURE", "severity": "blocker", "message": f"{source}: missing failure case."},
            ]}
        if role == "Momus" and "findings" in manifest:
            return {"decisions": [
                {"finding_id": finding["finding_id"], "decision": "already_covered", "citations": [{
                    "requirement_id": source, "element_id": element["id"], "section": "acceptance_method",
                    "quote": failure_quote if index == 1 else quote,
                }]}
                for index, finding in enumerate(manifest["findings"])
            ]}
        return output

    with workflow_model_server(bundle, transform=respond) as (url, calls):
        with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"))) as client:
            configure_roles(client, url, "Athena", "Momus")
            workflow_id = create(client, bundle.requirement)["workflow_id"]
            action(client, workflow_id, "approve_requirement", "requirement_approved")
            designed = action(client, workflow_id, "generate_design", "design_pending_approval")
            assert designed["design"]["version"] == 2
            assert corrected
            original, checked, carried, current = designed["design_review_history"]
            assert original["submission"]["findings"][0]["status"] == "open"
            assert checked["submission"]["findings"][0]["status"] == "closed"
            assert quote in checked["submission"]["findings"][0]["closure_reason"]
            assert checked["submission"]["findings"][1]["status"] == "open"
            assert carried["submission"]["findings"][1]["finding_id"] == checked["submission"]["findings"][1]["finding_id"]
            assert failure_quote in current["submission"]["findings"][1]["closure_reason"]
            assert current["review"]["verdict"] == "passed"
            assert len([call for call in calls if "findings" in call["manifest"] and call["role"] == "Momus"]) == 2


@pytest.mark.parametrize(("budget", "mode", "expected"), [
    (80, "fixed", "design_pending_approval"),
    (80, "regression", "design_pending_approval"),
    (80, "incomplete", "design_review_blocked"),
    (8, "incomplete", "error"),
])
def test_inventory_revision_processes_all_findings_in_one_action(tmp_path, bundle, budget, mode, expected):
    req = Requirement.model_validate_json((INVENTORY / "requirement.json").read_bytes())
    base_design, _, _, _ = inventory_profile(req)
    gaps = {
        "FR-005": "相同订单号、商品与数量重复预占，各状态均返回原订单且库存不变。",
        "FR-009": "每个并发可见快照均保持 initial=available+reserved+sold，三项非负。",
        "NFR-002": "每轮使用新实例，以百线程预占十件库存。连续五轮恰有十次成功。",
    }
    reported = [{"code": f"GAP-{source}", "severity": "blocker", "message": f"{source}：缺少{quote}"}
                for source, quote in gaps.items()]
    reviews = 0
    revision_checklists = []

    def respond(role, manifest, output):
        nonlocal reviews
        if role == "Athena":
            previous = manifest["previous_design"]
            if not previous:
                return {"design": base_design}
            revision_checklists.append(manifest["findings"])
            elements = list(previous["elements"])
            sources = []
            if len(revision_checklists) == 2:
                sources = ["FR-005"]
            elif len(revision_checklists) == 3 and mode in {"fixed", "regression"}:
                sources = ["FR-009", "NFR-002"]
                if mode == "regression":
                    elements = [element for element in elements if element["id"] != "D-CHECK-FR-005"]
            elif len(revision_checklists) == 4 and mode == "regression":
                sources = ["FR-005"]
            elements.extend({"id": f"D-CHECK-{source}", "sources": [source], "description": gaps[source],
                             "acceptance_method": gaps[source]} for source in sources)
            return {"design": {**previous, "elements": elements}}
        if "design_content" in manifest:
            reviews += 1
            return {"verdict": "blocked", "findings": reported} if reviews == 1 else output

        elements = {element["id"] for element in manifest["design"]["elements"]}
        decisions = []
        for finding in manifest["findings"]:
            source = next(source for source in gaps if source in finding["message"])
            element_id = f"D-CHECK-{source}"
            citations = [{"requirement_id": source, "element_id": element_id, "section": "acceptance_method",
                          "quote": gaps[source]}] if element_id in elements else []
            decisions.append({"finding_id": finding["finding_id"], "citations": citations,
                              "decision": "already_covered" if citations else "keep"})
        return {"decisions": decisions}

    with workflow_model_server(bundle, transform=respond) as (url, calls):
        with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"))) as client:
            configure_roles(client, url, "Athena", "Momus")
            created = client.post("/api/workflows", json={"requirement": req.model_dump(mode="json"),
                "allow_synthetic": True, "model_mode": "online", "call_budget": budget})
            assert created.status_code == 200
            workflow_id = created.json()["workflow_id"]
            action(client, workflow_id, "approve_requirement", "requirement_approved")
            blocked = action(client, workflow_id, "generate_design", "design_review_blocked")
            all_ids = [finding["finding_id"] for finding in blocked["design_review"]["submission"]["findings"]]
            assert len(all_ids) == 3
            revised = action(client, workflow_id, "generate_design", expected, operation_id="batch-1")
            assert [item["finding_id"] for item in revision_checklists[1]] == all_ids
            assert revised["design"]["version"] == revised["design_review"]["review"]["target"]["version"]
            assert len(calls) == revised["snapshot"]["calls_used"]
            assert reviews == 1
            if budget == 8:
                assert len(calls) == budget
                assert revised["error"]["code"] == "MODEL_BUDGET_EXHAUSTED"
                assert "approve_design" not in revised["allowed_actions"]
            else:
                assert [item["finding_id"] for item in revision_checklists[2]] == all_ids[1:]
                assert revised["design_revision"]["round"] == (2 if mode == "fixed" else 3)
                assert revised["design_review"]["blocking_finding_ids"] == (all_ids[1:] if mode == "incomplete" else [])
                assert ("approve_design" in revised["allowed_actions"]) == (mode != "incomplete")
                if mode == "regression":
                    assert [item["finding_id"] for item in revision_checklists[3]] == all_ids[:1]
                before = len(calls)
                action(client, workflow_id, "generate_design", expected, operation_id="batch-1")
                assert len(calls) == before


@pytest.mark.parametrize("break_traceability", [False, True])
def test_inventory_design_repairs_validation_order_and_keeps_advice_nonblocking(tmp_path, bundle, break_traceability):
    req = Requirement.model_validate_json((INVENTORY / "requirement.json").read_bytes())
    base_design, _, _, _ = inventory_profile(req)
    blocker = {"code": "MISSING-VALIDATION-ORDER", "severity": "blocker", "message":
               "FR-011：格式合法且订单参数冲突时，应先于商品或库存错误返回 ORDER_CONFLICT。"}
    advice = [{"code": "LOCK-DETAIL", "severity": "important", "message": "建议记录订单到商品的锁获取细节。"},
              {"code": "SNAPSHOT-WORDING", "severity": "important", "message": "建议展开当前订单快照的示例。"}]
    quote = "同订单号参数冲突且格式合法时，即使商品不存在或库存不足也返回 ORDER_CONFLICT；格式非法优先返回 INVALID_ARGUMENT。"

    def respond(role, manifest, output):
        if role == "Athena":
            if not manifest["previous_design"]:
                return {"design": base_design}
            assert [f["message"] for f in manifest["findings"]] == [blocker["message"]]
            return {"design": {**base_design, "elements": [*base_design["elements"], {
                "id": "D-VALIDATION-ORDER", "description": "按已批准顺序校验预占。",
                "sources": ["FR-011", "FR-UNKNOWN"] if break_traceability else ["FR-011"],
                "acceptance_method": quote,
            }]}}
        if "design_content" in manifest:
            if manifest["design"]["version"] == 1:
                return {"verdict": "blocked", "findings": [blocker, *advice]}
            # A second broad review could rephrase and re-raise the same issue.
            return {"verdict": "blocked", "findings": [{**blocker, "code": "RESERVE-CHECK-PRECEDENCE"}]}
        finding, = manifest["findings"]
        assert finding["message"] == blocker["message"]
        fixed = any(element["id"] == "D-VALIDATION-ORDER" for element in manifest["design"]["elements"])
        return {"decisions": [{"finding_id": finding["finding_id"], "decision": "already_covered" if fixed else "keep",
                               "citations": [{"requirement_id": "FR-011", "element_id": "D-VALIDATION-ORDER",
                                              "section": "acceptance_method", "quote": quote}] if fixed else []}]}

    with workflow_model_server(bundle, transform=respond) as (url, calls):
        with TestClient(create_app(store=Store(tmp_path / "state.sqlite3"))) as client:
            configure_roles(client, url, "Athena", "Momus")
            created = client.post("/api/workflows", json={"requirement": req.model_dump(mode="json"),
                "allow_synthetic": True, "model_mode": "online"})
            assert created.status_code == 200
            workflow_id = created.json()["workflow_id"]
            action(client, workflow_id, "approve_requirement", "requirement_approved")
            expected = "design_review_blocked" if break_traceability else "design_pending_approval"
            design = action(client, workflow_id, "generate_design", expected)
            assert design["design"]["version"] == 2
            report = design["design_review"]
            assert [f["category"] for f in report["submission"]["findings"]
                    if f["finding_id"] in report["blocking_finding_ids"]] == (["UNKNOWN_SOURCE"] if break_traceability else [])
            assert [f["message"] for f in report["submission"]["findings"] if f["severity"] == "important"] == [
                f["message"] for f in advice]
            assert len(calls) == 5
            assert sum("design_content" in call["manifest"] for call in calls) == 1
            if not break_traceability:
                action(client, workflow_id, "approve_design", "design_approved")


def test_plan_checklist_survives_restart_and_rejects_missing_evidence(tmp_path, bundle):
    revised_goal = "登记报名状态和可见返回值"
    blocked_message = "T01 must record the visible return value."

    def respond(role, manifest, output):
        if role == "Daedalus" and manifest.get("previous_tasks"):
            assert len(manifest["review_findings"]) == 1 and blocked_message in manifest["review_findings"][0]
            tasks = output["tasks"]
            return {"tasks": [{**tasks[0], "goal": revised_goal}, *tasks[1:]]}
        if role == "Momus" and "blockers" in manifest:
            finding, = manifest["blockers"]
            quote = revised_goal if manifest["plan"]["version"] == 3 else "This text is absent from the task."
            return {"decisions": [{"finding_id": finding["finding_id"], "decision": "resolved", "citations": [
                {"task_id": "T01", "field": "goal", "quote": quote},
            ]}]}
        if role == "Momus" and "plan" in manifest:
            return {"verdict": "blocked", "findings": [{
                "code": "RETURN-VALUE", "severity": "blocker", "task_id": "T01", "message": blocked_message,
            }]}
        return output

    database = tmp_path / "state.sqlite3"
    with workflow_model_server(bundle, transform=respond) as (url, calls):
        with TestClient(create_app(store=Store(database))) as client:
            configure_roles(client, url, "Athena", "Momus", "Daedalus")
            workflow_id = create(client, bundle.requirement)["workflow_id"]
            action(client, workflow_id, "approve_requirement", "requirement_approved")
            action(client, workflow_id, "generate_design", "design_pending_approval")
            action(client, workflow_id, "approve_design", "design_approved")
            blocked = action(client, workflow_id, "generate_plan", "plan_review_blocked")
            assert blocked["plan"]["version"] == 2
            assert not blocked["acceptance"]
            finding_id, = blocked["plan_review_record"]["blocking_finding_ids"]
        with TestClient(create_app(store=Store(database))) as client:
            result = action(client, workflow_id, "generate_plan", "plan_pending_approval", operation_id="plan-repair")
            assert result["plan"]["version"] == 3
            finding, = result["plan_review_record"]["submission"]["findings"]
            assert finding["finding_id"] == finding_id and finding["status"] == "closed"
            assert revised_goal in finding["closure_reason"]
            assert len(result["acceptance"]) == len(result["plan"]["tasks"])
            assert sum("plan" in call["manifest"] and "blockers" not in call["manifest"] for call in calls) == 1


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
            configure_roles(client, url, "Athena", "Momus")
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
    for role in ('Athena', 'Momus'):
        configured = client.put('/api/settings/agents/' + role, json={
            'base_url': base_url, 'model': 'local-' + role, 'api_key': 'child-only-key'})
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

"""Export canonical schema and synthetic fixtures; --check detects drift."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from olympus.contracts.models import (  # noqa: E402
    Artifact,
    Attempt,
    Command,
    Confirmation,
    ContractBundle,
    Design,
    Event,
    ExecutionRecord,
    Plan,
    Provenance,
    Receipt,
    Requirement,
    Review,
    Run,
    Task,
    TaskRuntime,
    fingerprint,
)


def build_fixture():
    provenance = [
        Provenance(kind="synthetic_fixture", source_id="H-01", locator="人工构造案例，非真实用户决定")
    ]
    req_content = {
        "requirements": [
            {"id": f"FR-{n:03}", "text": text, "acceptance": text}
            for n, text in enumerate(
                [
                    "最后名额并发不超容量",
                    "同账号不重复占位",
                    "自动递补最早有效候补",
                    "取消后退出有效队列",
                    "主动查询状态",
                ],
                1,
            )
        ],
        "out_of_scope": ["真实身份", "收费", "短信/推送"],
    }
    req = Requirement(
        object_id="I001",
        version=2,
        content=req_content,
        content_hash=fingerprint(req_content),
        provenance=provenance,
    )
    req.confirmation = Confirmation(
        confirmation_id="H-01",
        actor="YOU-fixture",
        origin="synthetic_fixture",
        target=req.ref(),
        dependencies=[],
        confirmed_at="2026-11-15T08:00:00Z",
    )
    elements = [
        {"id": i, "sources": src, "description": desc, "acceptance_method": "独立边界与并发检查"}
        for i, src, desc in [
            ("D-DATA-01", ["FR-002"], "活动/账号唯一"),
            ("D-SM-01", ["FR-003", "FR-004"], "CONFIRMED/WAITLISTED/CANCELLED，跳过取消者"),
            ("D-CON-01", ["FR-001", "FR-003"], "报名、取消、递补原子事务"),
            ("D-UI-01", ["FR-005"], "仅主动查询，不推送"),
        ]
    ]
    ds_content = dict(
        modules=["报名", "候补", "查询"],
        data=["activity", "registration"],
        interfaces=["join", "cancel", "query"],
        states=["CONFIRMED", "WAITLISTED", "CANCELLED"],
        concurrency="按活动事务互斥",
        exceptions=["容量已满", "重复报名"],
        interaction="主动查询与失败提示",
        decisions=["服务端顺序自动递补"],
        elements=elements,
    )
    ds = Design(
        object_id="DS-I001",
        version=1,
        content=ds_content,
        content_hash=fingerprint(ds_content),
        provenance=provenance,
        requirement=req.ref(),
    )
    definitions = [
        (
            "interface",
            "接口类型与测试夹具",
            [],
            "Hephaestus",
            ["FR-001", "FR-002", "FR-003", "FR-004", "FR-005"],
        ),
        ("implement", "报名与取消服务", ["T01"], "Hephaestus", ["FR-001", "FR-002", "FR-004"]),
        ("implement", "候补与自动递补服务", ["T01"], "Hephaestus", ["FR-003", "FR-004"]),
        ("implement", "报名与恢复交互", ["T01"], "Hephaestus", ["FR-001", "FR-002", "FR-004"]),
        ("implement", "状态查询页面", ["T03"], "Hephaestus", ["FR-005"]),
        (
            "test",
            "边界与并发测试",
            ["T02", "T03"],
            "Momus",
            ["FR-001", "FR-002", "FR-003", "FR-004", "FR-005"],
        ),
        (
            "integrate",
            "联调与恢复走查",
            ["T02", "T03", "T04", "T05"],
            "Hermes",
            ["FR-001", "FR-002", "FR-003", "FR-004", "FR-005"],
        ),
        (
            "doc",
            "验收汇总与交付清单",
            ["T06", "T07"],
            "Hermes",
            ["FR-001", "FR-002", "FR-003", "FR-004", "FR-005"],
        ),
    ]
    tasks = []
    for i, (kind, goal, deps, executor, sources) in enumerate(definitions, 1):
        tid = f"T{i:02}"
        data = dict(
            task_id=tid,
            type=kind,
            goal=goal,
            sources=sources + (["D-UI-01"] if tid in ("T04", "T05") else ["D-SM-01"]),
            inputs=[f"artifact:{dependency}" for dependency in deps],
            outputs=[f"artifact:{tid}"],
            depends_on=deps,
            executor=executor,
            reviewer="Argus",
            write_scope=[f"packages/{tid.lower()}"],
            checks=[f"check:{tid}"],
            resources=[],
            priority=0,
            policy={"schema_version": "1.0", "max_auto_retries": 2, "timeout_seconds": 120},
            required=True,
        )
        tasks.append(Task(**data, version=1, content_hash=fingerprint(data)))
    plan_content = {
        "tasks": [{"task_id": t.task_id, "version": t.version, "content_hash": t.content_hash} for t in tasks]
    }
    plan = Plan(
        object_id="PLAN-I001",
        version=1,
        content=plan_content,
        content_hash=fingerprint(plan_content),
        provenance=provenance,
        requirement=req.ref(),
        design=ds.ref(),
        tasks=tasks,
    )
    run = Run(
        run_id="RUN-campus",
        mode="mock",
        approved_package=[req.ref(), ds.ref(), plan.ref()],
        tasks=[TaskRuntime(task_id=t.task_id) for t in tasks],
    )
    command = Command(
        operation_id="OP-demo",
        run_id=run.run_id,
        actor="YOU-fixture",
        action="pause",
        target_id=run.run_id,
        expected_version=1,
        expected_state_revision=0,
        parameters={},
    )
    changed = run.model_dump(mode="json")
    changed.update(state_revision=1, event_seq=1, status="paused")
    event = Event(
        event_id="EV-1",
        run_id=run.run_id,
        event_seq=1,
        task_id=None,
        attempt_id=None,
        old_state=None,
        new_state=None,
        reason="synthetic pause",
        actor_type="human",
        occurred_at="2026-11-15T08:01:00Z",
        state_revision=1,
        snapshot=Run.model_validate(changed),
    )
    attempt = Attempt(
        attempt_id="ATT-1",
        logical_task_id="LOG-T01",
        run_id=run.run_id,
        task_id="T01",
        generation=1,
        input_hash=plan.content_hash,
        provider_request_id=None,
        idempotency_key="REQ-1",
        mode="mock",
        status="prepared",
    )
    artifact = Artifact(
        artifact_id="ART-T01",
        run_id=run.run_id,
        task_id="T01",
        attempt_id="ATT-1",
        location="packages/t01/types.json",
        content_hash=fingerprint({}),
        inputs=[ds.ref()],
        implementation_version="impl-v1",
        test_version="tests-v1",
        fixture_version="fixture-v1",
        mode="mock",
    )
    review = Review(
        review_id="REV-1",
        target=plan.ref(),
        author="Daedalus",
        reviewer="Momus",
        verdict="passed",
        findings=[],
        evidence=["fixture:plan-check"],
        mode="mock",
    )
    record = ExecutionRecord(
        record_id="EX-1",
        attempt_id="ATT-1",
        task_id="T01",
        inputs=[ds.ref()],
        tools=[],
        artifacts=[artifact.artifact_id],
        diff="",
        check_outputs=["synthetic fixture only"],
        elapsed_ms=0,
        call_count=0,
        retry_history=[],
        mode="mock",
    )
    return ContractBundle(
        requirement=req,
        design=ds,
        plan=plan,
        run=run,
        attempt=attempt,
        artifact=artifact,
        review=review,
        event=event,
        command=command,
        receipt=Receipt(
            operation_id=command.operation_id,
            run_id=run.run_id,
            status="pending",
            reason_code="FIXTURE_ONLY",
            state_revision=0,
        ),
        execution_record=record,
    )


def outputs():
    bundle = build_fixture()
    base = bundle.model_dump(mode="json")
    results = {
        "docs/contracts/schema.json": ContractBundle.model_json_schema(mode="serialization"),
        "fixtures/contracts/valid.json": base,
    }
    for name, value in base.items():
        if name == "schema_version":
            continue
        invalid = json.loads(json.dumps(value))
        invalid["schema_version"] = "0.9"
        results[f"fixtures/contracts/invalid/{name}.json"] = invalid
    for name, obj in {
        "task": bundle.plan.tasks[0],
        "task_runtime": bundle.run.tasks[0],
        "confirmation": bundle.requirement.confirmation,
    }.items():
        results[f"fixtures/contracts/valid/{name}.json"] = obj.model_dump(mode="json")
        results[f"fixtures/contracts/invalid/{name}.json"] = obj.model_dump(mode="json") | {
            "schema_version": "0.9"
        }
    scenarios = {}
    for name in [
        "normal",
        "blocked",
        "awaiting_review",
        "risk_accepted",
        "stale",
        "outcome_unknown",
        "completed",
    ]:
        state = json.loads(json.dumps(base["run"]))
        state["status"] = "active"
        first = state["tasks"][0]
        if name == "normal":
            first.update(state="accepted", verification_status="passed")
        elif name in ("blocked", "risk_accepted"):
            first.update(state="blocked", verification_status="failed")
            if name == "risk_accepted":
                first.update(disposition="risk_accepted", disposition_reason="构造：人工接受剩余风险")
        elif name == "awaiting_review":
            first.update(state="verifying")
        elif name == "stale":
            first.update(state="waiting", verification_status="stale", generation=2)
        elif name == "outcome_unknown":
            first.update(state="running")
        elif name == "completed":
            state["status"] = "completed"
            for task in state["tasks"]:
                task.update(state="accepted", verification_status="passed")
        scenarios[name] = Run.model_validate(state).model_dump(mode="json")
    results["fixtures/contracts/scenarios.json"] = scenarios
    results["fixtures/contracts/unknown_attempt.json"] = base["attempt"] | {
        "status": "outcome_unknown",
        "provider_request_id": "external-unknown",
    }
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for relative, value in outputs().items():
        path = ROOT / relative
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                raise SystemExit(f"generated contract drift: {relative}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    print(
        "Contracts and synthetic fixtures checked"
        if args.check
        else "Contracts and synthetic fixtures exported"
    )

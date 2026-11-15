"""Export deterministic planning fixtures; --check detects drift."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
from export_contracts import build_fixture  # noqa: E402
from olympus.contracts.models import Plan, Task, fingerprint  # noqa: E402
from olympus.planning.compiler import compile_plan  # noqa: E402
from olympus.planning.models import build_task_cards  # noqa: E402
from olympus.planning.validator import validate_plan_semantics  # noqa: E402
from olympus.traceability.planning import build_planning_traceability  # noqa: E402


def mutate(plan: Plan, name: str, changes: dict[str, dict], version: int | None = None) -> Plan:
    """Rebuild a plan from the baseline with per-task semantic changes."""
    tasks = []
    for task in plan.tasks:
        data = task.semantic_content()
        data.update(changes.get(task.task_id, {}))
        if data == task.semantic_content():
            tasks.append(task)
        else:
            tasks.append(Task(**data, version=task.version + 1, content_hash=fingerprint(data)))
    content = {
        "tasks": [
            {"task_id": task.task_id, "version": task.version, "content_hash": task.content_hash}
            for task in tasks
        ]
    }
    return Plan(
        object_id=f"PLAN-I001-{name.upper()}",
        version=plan.version + 1 if version is None else version,
        content=content,
        content_hash=fingerprint(content),
        provenance=plan.provenance,
        requirement=plan.requirement,
        design=plan.design,
        tasks=tasks,
    )


COUNTEREXAMPLES = {
    "cycle": {
        "description": "T02 与 T05 互相依赖，形成 T02 -> T05 -> T02 环",
        "expected_codes": ["CYCLE_DETECTED"],
        "changes": {"T02": {"depends_on": ["T01", "T05"]}, "T05": {"depends_on": ["T02", "T03"]}},
    },
    "missing_source": {
        "description": "T05 引用了不存在的 FR-999",
        "expected_codes": ["MISSING_SOURCE"],
        "changes": {"T05": {"sources": ["FR-999", "FR-005", "D-UI-01"]}},
    },
    "missing_test": {
        "description": "T06 漏掉 FR-003，导致 FR-003 没有测试覆盖",
        "expected_codes": ["MISSING_TEST_COVERAGE"],
        "changes": {"T06": {"sources": ["FR-001", "FR-002", "FR-004", "FR-005", "D-SM-01"]}},
    },
    "out_of_scope_sms": {
        "description": "T05 目标出现范围外能力“短信/推送通知候补结果”",
        "expected_codes": ["OUT_OF_SCOPE_VIOLATION"],
        "changes": {"T05": {"goal": "短信/推送通知候补结果"}},
    },
    "acyclic_missing_input": {
        "description": "DAG 无环，但 T05 只依赖 T01 却要求 artifact:T03（候补状态查询输入）",
        "expected_codes": ["MISSING_INPUT_SUPPLY"],
        "changes": {"T05": {"depends_on": ["T01"]}},
    },
}


def build_counterexamples(req, ds, plan):
    counterexamples = {}
    for name, spec in COUNTEREXAMPLES.items():
        counterexample = mutate(plan, name, spec["changes"])
        report = validate_plan_semantics(counterexample, req, ds)
        codes = sorted({issue.code for issue in report.issues})
        if codes != sorted(spec["expected_codes"]):
            raise SystemExit(f"counterexample {name} produced {codes}, expected {spec['expected_codes']}")
        counterexamples[name] = {
            "description": spec["description"],
            "expected_codes": spec["expected_codes"],
            "plan": counterexample.model_dump(mode="json"),
        }
    return counterexamples


def outputs():
    bundle = build_fixture()
    req, ds, plan = bundle.requirement, bundle.design, bundle.plan
    cards = build_task_cards(plan, req, ds)
    manifest = compile_plan(plan, req, ds)
    index = build_planning_traceability(req, ds, plan)
    return {
        "fixtures/plans/standard_plan.json": {
            "edge_count": sum(len(task.depends_on) for task in plan.tasks),
            "plan": plan.model_dump(mode="json"),
            "task_cards": [card.model_dump(mode="json") for card in cards],
        },
        "fixtures/plans/compiled_manifest.json": manifest.model_dump(mode="json"),
        "fixtures/plans/traceability_matrix.json": index.model_dump(mode="json"),
        "fixtures/plans/counterexamples.json": build_counterexamples(req, ds, plan),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for relative, value in outputs().items():
        path = ROOT / relative
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                raise SystemExit(f"generated planning fixture drift: {relative}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    print(
        "Planning fixtures checked" if args.check else "Planning fixtures exported"
    )

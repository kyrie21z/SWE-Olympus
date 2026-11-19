"""Delivery requirements need matching task owners without replacing business code."""

from copy import deepcopy

import pytest

from olympus.contracts.models import Design, Plan, Requirement, Task, fingerprint
from olympus.planning.service import PlanService
from olympus.planning.validator import coverage_obligations, validate_plan_semantics
from olympus.traceability.planning import build_planning_traceability


def rebuild_plan(plan, tasks, *, req=None, design=None):
    content = {"tasks": [{"task_id": task.task_id, "version": task.version, "content_hash": task.content_hash}
                         for task in tasks]}
    return Plan.model_validate({
        **plan.model_dump(mode="json"), "tasks": tasks, "content": content, "content_hash": fingerprint(content),
        "requirement": (req.ref() if req else plan.requirement),
        "design": (design.ref() if design else plan.design),
    })


def change_task(task, **changes):
    content = {**task.semantic_content(), **changes}
    return Task(**content, version=task.version + 1, content_hash=fingerprint(content))


@pytest.fixture
def delivery_plan(bundle):
    req_content = {"requirements": [
        {"id": "FR-001", "text": "创建商品并返回库存。", "acceptance": "自动化测试检查创建后的库存。"},
        {"id": "NFR-002", "text": "提供可重复执行的自动化测试与中文运行说明。",
         "acceptance": "unittest 覆盖正常、边界、非法状态、幂等、隔离、并发；100请求抢10件连续5轮通过。"},
    ], "out_of_scope": []}
    req = Requirement.model_validate({
        **bundle.requirement.model_dump(mode="json"), "content": req_content,
        "content_hash": fingerprint(req_content), "confirmation": None,
    })
    design_content = deepcopy(bundle.design.content)
    design_content["elements"] = [
        {"id": "ELEM-CORE", "sources": ["FR-001"], "description": "库存业务接口与状态迁移。",
         "acceptance_method": "自动化测试和中文运行说明。"},
        {"id": "ELEM-TESTING", "sources": ["NFR-002"],
         "description": "unittest suite covering normal, boundary, illegal, idempotent, isolation, concurrency; "
                        "5 rounds of 100-req test",
         "acceptance_method": "检查用例、报告与运行说明。"},
    ]
    design = Design.model_validate({
        **bundle.design.model_dump(mode="json"), "content": design_content,
        "content_hash": fingerprint(design_content), "requirement": req.ref(),
    })
    specs = [
        ("implement", ["FR-001", "ELEM-CORE"], "Hephaestus"),
        ("test", ["FR-001", "NFR-002", "ELEM-CORE"], "Momus"),
        ("integrate", ["NFR-002", "ELEM-TESTING"], "Hermes"),
        ("doc", ["NFR-002", "ELEM-TESTING"], "Hermes"),
    ]
    tasks = []
    for index, (kind, sources, executor) in enumerate(specs, 1):
        previous = f"T{index-1:02}" if index > 1 else None
        content = {
            **bundle.plan.tasks[0].semantic_content(), "task_id": f"T{index:02}", "type": kind,
            "goal": f"{kind} 交付", "sources": sources, "executor": executor, "reviewer": "Argus",
            "inputs": [f"artifact:{previous}"] if previous else [],
            "outputs": [f"artifact:T{index:02}"], "depends_on": [previous] if previous else [],
            "checks": [f"check:T{index:02}"], "write_scope": [f"packages/t{index:02}"],
        }
        tasks.append(Task(**content, version=1, content_hash=fingerprint(content)))
    return req, design, rebuild_plan(bundle.plan, tasks, req=req, design=design)


def test_test_and_doc_delivery_is_covered_without_fake_implementation_task(delivery_plan):
    req, design, plan = delivery_plan
    report = validate_plan_semantics(plan, req, design)
    assert report.valid, report.issues
    assert PlanService().prepare(req, design, plan).validation.valid
    index = build_planning_traceability(req, design, plan)
    assert index.coverage_complete
    delivery = next(row for row in index.rows if row.req_id == "NFR-002")
    assert delivery.stage_status == "planned"
    assert delivery.implement_tasks == []
    assert delivery.test_tasks == ["T02"] and delivery.doc_tasks == ["T04"]
    assert index.uncovered_requirements == index.uncovered_design_elements == []


@pytest.mark.parametrize("missing", ["test", "doc"])
def test_combined_delivery_needs_both_cited_task_categories(delivery_plan, missing):
    req, design, plan = delivery_plan
    removed_types = {"test", "integrate"} if missing == "test" else {"doc"}
    tasks = [change_task(task, sources=["FR-001", "ELEM-CORE"]) if task.type in removed_types else task
             for task in plan.tasks]
    plan = rebuild_plan(plan, tasks)
    report = validate_plan_semantics(plan, req, design)
    assert ("UNCOVERED_REQUIREMENT", "NFR-002") in {(issue.code, issue.source_id) for issue in report.issues}
    index = build_planning_traceability(req, design, plan)
    assert index.uncovered_requirements == ["NFR-002"]
    assert next(row for row in index.rows if row.req_id == "NFR-002").stage_status == "missing_coverage"


def test_docs_and_tests_cannot_replace_business_implementation(delivery_plan):
    req, design, plan = delivery_plan
    tasks = [change_task(task, type="doc", executor="Hermes") if task.type == "implement" else task
             for task in plan.tasks]
    plan = rebuild_plan(plan, tasks)
    report = validate_plan_semantics(plan, req, design)
    issues = {(issue.code, issue.source_id) for issue in report.issues}
    assert ("UNCOVERED_REQUIREMENT", "FR-001") in issues
    assert ("UNCOVERED_DESIGN", "ELEM-CORE") in issues
    index = build_planning_traceability(req, design, plan)
    row = next(row for row in index.rows if row.req_id == "FR-001")
    assert row.all_tasks and row.stage_status == "missing_coverage"


@pytest.mark.parametrize(("text", "expected"), [
    ("提供可重复执行的自动化测试与中文运行说明。", ("test", "doc")),
    ("Provide repeatable automated tests and Chinese run instructions.", ("test", "doc")),
    ("unittest suite covering normal and boundary cases", ("test",)),
    ("At least six unit test tasks covering three modules independently", ("test",)),
    ("Unit tests for equipment_catalog module producing pass report", ("test",)),
    ("Integration task registers two devices and checks the full loan lifecycle", ("test",)),
    ("Integration acceptance registers two devices and runs the loan lifecycle", ("test",)),
    ("集成验收覆盖登记、借还和统计。", ("test",)),
    ("提供中文运行说明。", ("doc",)),
    ("提供库存服务及自动化测试与中文运行说明。", ("implementation",)),
    ("提供自动化测试并实现库存预占。", ("implementation",)),
    ("Provide automated tests and implement stock reservations.", ("implementation",)),
    ("查询库存，使用自动化测试和文档说明验证。", ("implementation",)),
    ("提供文档检索服务。", ("implementation",)),
    ("并发预占必须保持库存守恒。", ("implementation",)),
])
def test_only_explicit_primary_deliverables_expand_task_types(text, expected):
    assert coverage_obligations(text) == expected


def test_nfr_prefix_and_acceptance_keywords_do_not_classify_business_work(delivery_plan):
    req, design, plan = delivery_plan
    content = deepcopy(req.content)
    content["requirements"][1]["text"] = "并发预占必须保持库存守恒。"
    req = Requirement.model_validate({
        **req.model_dump(mode="json"), "content": content, "content_hash": fingerprint(content),
    })
    design = Design.model_validate({**design.model_dump(mode="json"), "requirement": req.ref()})
    plan = rebuild_plan(plan, plan.tasks, req=req, design=design)
    report = validate_plan_semantics(plan, req, design)
    assert ("UNCOVERED_REQUIREMENT", "NFR-002") in {(issue.code, issue.source_id) for issue in report.issues}

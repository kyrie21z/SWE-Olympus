"""人工请求存储的单测（issue #6 的 `tests/human/`）。

存储层要保证三件事：
1. `prepare` 幂等——同一 (run, kind, target) 只建一次，重放不产生新请求；
2. `answer` / `accept` 幂等——重复提交返回同一对象，不覆盖；
3. **落盘可恢复**——换一个实例从同一文件读回同样的请求。
"""

from __future__ import annotations

import json

import pytest
from olympus.human.store import HumanRequestStore


def test_prepare_is_idempotent():
    store = HumanRequestStore()
    first = store.prepare(run_id="RUN-1", kind="plan_approval", target_id="PLAN-1",
                          expected_version=1, expected_state_revision=0)
    again = store.prepare(run_id="RUN-1", kind="plan_approval", target_id="PLAN-1",
                          expected_version=1, expected_state_revision=0)
    assert first.request_id == again.request_id
    assert len(store.all()) == 1


def test_different_kind_creates_new_request():
    store = HumanRequestStore()
    a = store.prepare(run_id="RUN-1", kind="plan_approval", target_id="PLAN-1",
                      expected_version=1, expected_state_revision=0)
    b = store.prepare(run_id="RUN-1", kind="delivery_confirm", target_id="RUN-1",
                      expected_version=1, expected_state_revision=0)
    assert a.request_id != b.request_id
    assert len(store.pending("RUN-1")) == 2


def test_answer_is_idempotent():
    store = HumanRequestStore()
    request = store.prepare(run_id="RUN-1", kind="plan_approval", target_id="PLAN-1",
                            expected_version=1, expected_state_revision=0)
    first = store.answer(request.request_id, {"decision": "approve"})
    second = store.answer(request.request_id, {"decision": "reject"})
    assert first.status == second.status == "answered"
    assert second.answer == {"decision": "approve"}, "已答过的不被覆盖"


def test_is_current_reflects_content_version():
    store = HumanRequestStore()
    request = store.prepare(run_id="RUN-1", kind="plan_approval", target_id="PLAN-1",
                            expected_version=1, expected_state_revision=3)
    assert store.is_current(request.request_id, state_revision=3, version=1)
    assert not store.is_current(request.request_id, state_revision=4, version=1)
    assert not store.is_current(request.request_id, state_revision=3, version=2)


def test_mark_stale_keeps_history():
    store = HumanRequestStore()
    request = store.prepare(run_id="RUN-1", kind="plan_approval", target_id="PLAN-1",
                            expected_version=1, expected_state_revision=0)
    store.mark_stale(request.request_id)
    assert store.load(request.request_id).status == "stale"
    assert request.request_id in [r.request_id for r in store.all()], "作废不等于删除"


def test_persists_across_instances(tmp_path):
    path = tmp_path / "human.json"
    store = HumanRequestStore(path)
    request = store.prepare(run_id="RUN-1", kind="plan_approval", target_id="PLAN-1",
                            expected_version=1, expected_state_revision=0)

    reloaded = HumanRequestStore(path)
    assert reloaded.load(request.request_id).kind == "plan_approval"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert request.request_id in on_disk


def test_unknown_request_raises():
    store = HumanRequestStore()
    with pytest.raises(KeyError):
        store.load("HR-nope")

# ---------------------------------------------------------------- 绑定与报告（issue #6 工作包 1）
def test_request_binds_req_ds_plan():
    """关口绑定的是"哪一版需求/设计/计划"，不是一个模糊的 target_id。"""
    store = HumanRequestStore()
    req = store.prepare(
        run_id="RUN-1", kind="plan_approval", target_id="PLAN-1",
        expected_version=1, expected_state_revision=3,
        req_ref={"object_id": "I001", "version": 2, "content_hash": "a" * 64},
        ds_ref={"object_id": "DS-I001", "version": 1, "content_hash": "b" * 64},
        plan_ref={"object_id": "PLAN-1", "version": 1, "content_hash": "c" * 64},
    )
    assert req.req_ref["object_id"] == "I001"
    assert req.ds_ref["object_id"] == "DS-I001"
    assert req.plan_ref["object_id"] == "PLAN-1"


def test_prepare_refuses_when_reports_missing():
    """必要报告不齐就不让开关口——不能在人没依据时让人拍板。"""
    store = HumanRequestStore()
    with pytest.raises(ValueError) as exc:
        store.prepare(
            run_id="RUN-1", kind="plan_approval", target_id="PLAN-1",
            expected_version=1, expected_state_revision=0,
            required_reports=["review:engineering", "review:momus"],
            available_reports=["review:engineering"],
        )
    assert "review:momus" in str(exc.value)
    assert store.all() == [], "拒绝时不留下半个请求"


def test_prepare_accepts_when_reports_complete():
    store = HumanRequestStore()
    req = store.prepare(
        run_id="RUN-1", kind="plan_approval", target_id="PLAN-1",
        expected_version=1, expected_state_revision=0,
        required_reports=["review:engineering", "review:momus"],
        available_reports=["review:engineering", "review:momus"],
    )
    assert req.required_reports == ["review:engineering", "review:momus"]

import importlib.util
from pathlib import Path

import pytest


@pytest.mark.scenario
def test_req_design_review_approval_plan_real_execution_and_evidence(tmp_path):
    path = Path(__file__).absolute().parents[3] / "scripts/p4_demo.py"
    spec = importlib.util.spec_from_file_location("p4_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.demonstrate(tmp_path / "demo")
    assert report["source"] == "synthetic_fixture"
    assert report["model_mode"] == "mock" and report["tool_mode"] == "real_tool"
    assert report["runtime"]["progress"] == {"accepted": 2, "required": 8}
    assert next(t for t in report["runtime"]["tasks"] if t["task_id"] == "T05")["state"] == "ready"
    assert not report["run_completed"]
    assert len(report["executions"]) == 2
    assert (tmp_path / "demo/report.json").exists()

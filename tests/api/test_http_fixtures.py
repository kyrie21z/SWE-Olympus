"""录制夹具的漂移检查。

`fixtures/http/*.json` 是 P6 的回放数据；接口一改，这里必须失败，
提醒作者重新生成并提交差异，而不是让前端拿着过期样例。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_recorder():
    spec = importlib.util.spec_from_file_location(
        "export_api_fixtures", ROOT / "scripts" / "export_api_fixtures.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_recorded_fixtures_match_live_api():
    recorder = load_recorder()

    recorded = recorder.record()

    for name, payload in recorded.items():
        path = ROOT / "fixtures" / "http" / f"{name}.json"
        assert path.exists(), f"缺少夹具 {name}.json"
        assert json.loads(path.read_text(encoding="utf-8")) == payload, f"夹具已漂移：{name}"


def test_fixtures_expose_durability_and_errors():
    fixtures = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in (ROOT / "fixtures" / "http").glob("*.json")
    }

    assert fixtures["command_start"]["headers"]["x-olympus-durability"] == "sqlite"
    assert fixtures["command_start"]["body"]["status"] == "accepted"
    assert fixtures["command_stale"]["body"]["reason_code"] == "stale"
    assert fixtures["snapshot"]["body"]["status"] == "paused"
    assert fixtures["events"]["body"][0]["snapshot"]["status"] == "paused"
    assert fixtures["trace"]["body"]["plan_ref"]["version"] == 1

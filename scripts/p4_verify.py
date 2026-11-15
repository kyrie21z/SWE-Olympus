"""记录 P4 可重现检查；完整后端检查需要安装项目锁定依赖。"""

import argparse
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).absolute().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--components-only", action="store_true",
                        help="仅运行 P4 组件检查，不代表整个项目集成通过")
    parser.add_argument("--output", type=Path, default=ROOT / ".olympus/p4-verification.json")
    args = parser.parse_args()
    environment = os.environ.copy()
    environment.update(PYTHONUTF8="1", PYTHONPATH=str(ROOT / "backend"), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                       LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    temp = ROOT / ".olympus/test-temp"
    temp.mkdir(parents=True, exist_ok=True)
    environment.update(TEMP=str(temp), TMP=str(temp))
    tests = ["tests/design", "tests/execution", "tests/scenarios/design", "tests/scenarios/execution"]
    commands = [
        [sys.executable, "scripts/export_contracts.py", "--check"],
        [sys.executable, "scripts/export_plans.py", "--check"],
        [sys.executable, "-m", "ruff", "check", "backend", "tests", "scripts"],
        [sys.executable, "-m", "pytest", *(tests if args.components_only else ["tests"]), "-q",
         "--basetemp=.olympus/verify-pytest", "-o", "cache_dir=.olympus/pytest-cache"],
    ]
    results = []
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        results.append({"command": command, "exit_code": result.returncode,
                        "stdout": result.stdout, "stderr": result.stderr})
    versions = {}
    for name in ["pytest", "pydantic", "ruff", "langgraph"]:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    available = importlib.util.find_spec("langgraph") is not None
    report = {"python": sys.version, "platform": platform.platform(), "versions": versions,
              "components_only": args.components_only, "langgraph_available": available,
              "all_invoked_checks_passed": all(r["exit_code"] == 0 for r in results),
              "backend_suite_verified": not args.components_only and available and
                  all(r["exit_code"] == 0 for r in results),
              "frontend_checked": False, "browser_checked": False, "full_project_verified": False,
              "results": results, "online_model_evaluated": False,
              "note": "本脚本不运行前端和浏览器检查；组件通过或跳过用例不代表完整项目验证通过。"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("检查报告：", args.output)
    return 0 if report["all_invoked_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

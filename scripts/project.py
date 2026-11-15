"""Cross-platform commands shared by local development and CI."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LOCAL_PYTHON = ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
PYTHON = str(LOCAL_PYTHON) if LOCAL_PYTHON.exists() else sys.executable
NPM = shutil.which("npm.cmd" if os.name == "nt" else "npm")


def run(args, cwd=ROOT):
    env = os.environ.copy()
    env.update(PYTHONPATH=str(ROOT / "backend"), LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    print("Running:", " ".join(str(a) for a in args), flush=True)
    subprocess.run(args, cwd=cwd, env=env, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "test", "scenario", "browser", "backend", "frontend"])
    action = parser.parse_args().action
    try:
        if action == "backend":
            run([PYTHON, "-m", "uvicorn", "olympus.app:app", "--host", "127.0.0.1", "--port", "8000"])
        elif action == "frontend":
            run([NPM, "run", "dev"], ROOT / "frontend")
        elif action == "test":
            run([PYTHON, "-m", "pytest", "-q"])
        elif action == "scenario":
            run([PYTHON, "-m", "pytest", "tests/scenarios", "-q"])
        elif action == "browser":
            run([NPM, "run", "test:browser"], ROOT / "frontend")
        elif action == "check":
            run([PYTHON, "-m", "ruff", "check", "backend", "tests", "scripts"])
            run([PYTHON, "-m", "pytest", "-q"])
            for script in ["build"]:
                run([NPM, "run", script], ROOT / "frontend")
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode)

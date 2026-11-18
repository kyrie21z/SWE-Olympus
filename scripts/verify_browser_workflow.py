"""Verify the complete browser/API/eight-task workflow using an isolated local model server.

Run after installing Python/npm dependencies and the Playwright Chromium browser.
An installed Chrome may be selected with PLAYWRIGHT_CHANNEL=chrome. Evidence goes
to .olympus; all fake keys, runtime files and test data stay in a temporary database.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import UTC, datetime
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn  # noqa: E402

from olympus.app import create_app  # noqa: E402
from olympus.contracts.models import ContractBundle  # noqa: E402
from olympus.storage.sqlite import Store  # noqa: E402


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def request_json(url, *, method="GET", body=None):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8") if body is not None else None,
        method=method, headers={"Content-Type": "application/json"},
    )
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
        return json.loads(response.read())


def wait_http(url, *, process=None, thread=None, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("Vite exited before becoming ready; see vite.log")
        if thread is not None and not thread.is_alive():
            raise RuntimeError("Temporary backend exited before becoming ready")
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=1):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise TimeoutError(f"local test server did not become ready: {url}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mode", choices=("online", "offline"), default="online")
    args = parser.parse_args()
    output = (args.output or ROOT / ".olympus" / (
        "browser-workflow-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6]
    )).resolve()
    output.mkdir(parents=True, exist_ok=False)
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is required")
    fixture_path = ROOT / "tests/workflows/test_browser_host.py"
    specification = importlib.util.spec_from_file_location("olympus_browser_test_model", fixture_path)
    fixture = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(fixture)
    bundle = ContractBundle.model_validate_json((ROOT / "fixtures/contracts/valid.json").read_text("utf-8"))
    environment = os.environ.copy()
    environment.update(VITE_API_BASE_URL="", LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")

    with tempfile.TemporaryDirectory(prefix="runtime-", dir=output) as temporary:
        temporary = Path(temporary)
        req_path = temporary / "requirement.json"
        if args.mode == "offline":
            inventory = ROOT / "fixtures/requirements/inventory-reservation/requirement.json"
            shutil.copyfile(inventory, req_path)
        else:
            req_path.write_text(bundle.requirement.model_dump_json(indent=2), encoding="utf-8")
        app = create_app(store=Store(temporary / "state.sqlite3"))
        backend_port = free_port()
        backend_url = f"http://127.0.0.1:{backend_port}"
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=backend_port, log_level="warning", access_log=False,
        ))
        backend = threading.Thread(target=server.run, daemon=True)
        backend.start()
        vite = None
        try:
            wait_http(backend_url + "/health", thread=backend)
            provider = fixture.workflow_model_server(bundle) if args.mode == "online" else nullcontext(("", []))
            with provider as (model_url, calls):
                roles = ("Daedalus", "Momus", "Hephaestus", "Argus", "Hermes") if model_url else ()
                for role in roles:
                    request_json(backend_url + f"/api/settings/agents/{role}", method="PUT", body={
                        "base_url": model_url, "model": f"local-{role}", "api_key": f"browser-test-key-{role}",
                    })
                frontend_port = free_port()
                frontend_url = f"http://127.0.0.1:{frontend_port}"
                environment["OLYMPUS_BACKEND_URL"] = backend_url
                with (output / "vite.log").open("w", encoding="utf-8") as vite_log:
                    vite = subprocess.Popen(
                        [node, str(ROOT / "frontend/node_modules/vite/bin/vite.js"),
                         "--host", "127.0.0.1", "--port", str(frontend_port), "--strictPort"],
                        cwd=ROOT / "frontend", env=environment,
                        stdout=vite_log, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    wait_http(frontend_url, process=vite)
                    result = subprocess.run(
                        [node, str(ROOT / "frontend/tests/integration/real-workflow.mjs"),
                         frontend_url, model_url, str(req_path), str(output), args.mode],
                        cwd=ROOT, env=environment, timeout=240,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    summary = {
                        "total": len(calls),
                        "by_role": {role: sum(call["role"] == role for call in calls)
                                    for role in sorted({call["role"] for call in calls})},
                        "online_model_evaluated": False,
                    }
                    (output / "provider-calls.json").write_text(
                        json.dumps(summary, indent=2), encoding="utf-8",
                    )
                    if result.returncode and (output / "report.json").exists():
                        failure = json.loads((output / "report.json").read_text(encoding="utf-8"))
                        print(failure.get("error", "Browser workflow failed"), flush=True)
                    print(f"Browser workflow evidence: {output}", flush=True)
                    return result.returncode
        finally:
            # Only the Vite process created above is terminated. No port-based or
            # global process cleanup can affect the user's running workspace.
            if vite is not None and vite.poll() is None:
                vite.terminate()
                try:
                    vite.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    vite.kill()
                    vite.wait(timeout=5)
            server.should_exit = True
            backend.join(timeout=20)
            if backend.is_alive():
                raise RuntimeError("Temporary backend did not shut down cleanly")


if __name__ == "__main__":
    raise SystemExit(main())

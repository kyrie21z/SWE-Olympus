"""Run the real P5 API against the repository contract fixture.

This is a local integration entry for P6. It creates a temporary SQLite store,
attaches the approved PLAN and traceability documents, then serves the normal
P5 FastAPI app. No frontend fixture is injected.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from olympus.app import create_app  # noqa: E402
from olympus.contracts.models import ContractBundle  # noqa: E402
from olympus.planning.service import PlanService  # noqa: E402
from olympus.storage.sqlite import Store  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    bundle = ContractBundle.model_validate(
        json.loads((ROOT / "fixtures/contracts/valid.json").read_text(encoding="utf-8"))
    )
    database = ROOT / ".olympus" / f"p6-demo-{uuid4().hex}.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)

    store = Store(database)
    store.create(bundle.run)
    app = create_app(store=store)
    runtime = app.state.runs.registry.get(bundle.run.run_id)
    package = PlanService().prepare(
        bundle.requirement,
        bundle.design,
        bundle.plan,
    )
    runtime.attach_plan(
        bundle.plan,
        package.manifest,
        requirement=bundle.requirement,
        design=bundle.design,
    )

    print(f"P6 demo run: {bundle.run.run_id}", flush=True)
    print(f"P6 database: {database}", flush=True)
    print(
        f"P6 view: http://127.0.0.1:{args.port}/api/runs/{bundle.run.run_id}/view",
        flush=True,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()

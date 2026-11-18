"""FastAPI 应用入口。

`create_app(store=...)` 允许测试注入临时数据库；生产入口 `app` 不落盘，
数据库在第一次请求时才打开（见 `api.service.build_service`）。
"""

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from olympus.api.errors import install_error_handlers
from olympus.api.routes import router
from olympus.api.settings_routes import router as settings_router
from olympus.api.workflow_routes import router as workflow_router
from olympus.api.service import RunQueryService
from olympus.commands.runtime import CommandService
from olympus.commands.runtime.journal import StoreJournal
from olympus.storage.sqlite import Store


def create_app(*, store: Store | None = None, execution_view=None, runner_factory=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        yield
        workflows = getattr(app.state, "workflows", None)
        if workflows is not None:
            workflows.close()

    app = FastAPI(title="SWE-Olympus", version="0.3.0", lifespan=lifespan)
    app.state.execution_view = execution_view
    app.state.workflow_runner_factory = runner_factory
    app.state.services_lock = threading.RLock()

    if store is not None:
        runs = RunQueryService(store, execution_view=execution_view)
        app.state.store = store
        app.state.runs = runs
        app.state.commands = CommandService(runs.registry, journal=StoreJournal(store))

    install_error_handlers(app)
    app.include_router(router)
    app.include_router(settings_router)
    app.include_router(workflow_router)

    @app.get("/health")
    def health():
        return {"status": "ok", "stage": "P5-api", "schema_version": "1.0", "model_required": False}

    return app


app = create_app()

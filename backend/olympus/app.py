"""FastAPI 应用入口。

`create_app(store=...)` 允许测试注入临时数据库；生产入口 `app` 不落盘，
数据库在第一次请求时才打开（见 `api.service.build_service`）。
"""

from fastapi import FastAPI

from olympus.api.errors import install_error_handlers
from olympus.api.routes import router
from olympus.api.service import RunQueryService
from olympus.commands.runtime import CommandService
from olympus.storage.sqlite import Store


def create_app(*, store: Store | None = None, execution_view=None) -> FastAPI:
    app = FastAPI(title="SWE-Olympus", version="0.2.0")
    app.state.execution_view = execution_view

    if store is not None:
        runs = RunQueryService(store, execution_view=execution_view)
        app.state.store = store
        app.state.runs = runs
        app.state.commands = CommandService(runs.registry)

    install_error_handlers(app)
    app.include_router(router)

    @app.get("/health")
    def health():
        return {"status": "ok", "stage": "P5-api", "schema_version": "1.0", "model_required": False}

    return app


app = create_app()

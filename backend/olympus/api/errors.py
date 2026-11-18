"""统一的 HTTP 错误信封。

所有失败都返回同一个形状，调用方不需要按端点猜：

    {"error": {"code": "RUN_NOT_FOUND", "message": "...", "detail": {...}}}

`code` 是稳定的机器可读标识，`message` 给人看，`detail` 放排查用的事实。
刻意不回传堆栈：内部路径、SQL、对象结构都不该出现在接口上。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """可预期的协议错误：由调用方输入或当前状态导致，不是程序崩溃。"""

    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class RunNotFound(ApiError):
    status_code = 404
    code = "RUN_NOT_FOUND"


class InvalidCursor(ApiError):
    status_code = 422
    code = "INVALID_CURSOR"


class CursorAhead(ApiError):
    """游标超过服务端已提交序号。

    出现这种请求说明调用方手里的状态不可信（例如连到了另一个数据库），
    正确做法是重新取快照，而不是返回一个空列表让前端以为"没有新事件"。
    """

    status_code = 409
    code = "CURSOR_AHEAD"


class RecoveryBlocked(ApiError):
    """事件链无法证明完整：服务端拒绝提供状态，而不是给出一份半成品。"""

    status_code = 503
    code = "RECOVERY_GAP"


class TraceUnavailable(ApiError):
    """没有需求/设计原文，无法构建规划追踪索引。"""

    status_code = 409
    code = "TRACE_UNAVAILABLE"


class ExecutionViewUnavailable(ApiError):
    """宿主未注入执行查询服务，无法提供工件与执行详情。"""

    status_code = 409
    code = "EXECUTION_VIEW_UNAVAILABLE"


class ExecutionNotFound(ApiError):
    """指定的任务或尝试没有可查询的执行记录。"""

    status_code = 404
    code = "EXECUTION_NOT_FOUND"


def _envelope(code: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "detail": detail or {}}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 参数类型错误（例如 after_seq=abc）也走同一个信封，不让框架自带格式漏出去。
        return JSONResponse(
            status_code=422,
            content=_envelope(
                "INVALID_ARGUMENT",
                "请求参数不合法",
                {"errors": jsonable_encoder([
                    {"loc": error["loc"], "type": error["type"], "msg": "字段不符合要求"}
                    for error in exc.errors()
                ])},
            ),
        )

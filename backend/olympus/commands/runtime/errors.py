"""命令服务用到的领域错误。

这些错误直接继承 `ApiError`，好处是 HTTP 层不需要再映射一遍，
坏处是领域层依赖了 HTTP 的错误形状。对课程规模的项目，
换取"所有失败都长一个样"的一致性更划算；如果以后要复用到别的入口，
把 `ApiError` 换成纯领域异常再在适配层映射即可。
"""

from __future__ import annotations

from typing import Any

from olympus.api.errors import ApiError


class PlanNotBound(ApiError):
    """运行态存在，但还没有绑定已批准的计划，因此不能执行任何命令。"""

    status_code = 409
    code = "PLAN_NOT_BOUND"

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message, detail=detail)


class CommandPersistenceUnavailable(ApiError):
    """No success is returned when an atomic command transaction fails."""

    status_code = 503
    code = "COMMAND_PERSISTENCE_UNAVAILABLE"


class CommandStateConflict(ApiError):
    status_code = 409
    code = "COMMAND_STATE_CONFLICT"

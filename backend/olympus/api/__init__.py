"""HTTP 接入层。

这个包只负责"把已有能力暴露成接口"：
- 不重新判断业务规则（谁能批准、状态能不能转移，归 P3/P4）；
- 不拼接视图（最新快照 + 旧游标拼不成一个可信画面）；
- 不向浏览器暴露 Store.commit 这类受信写入口。
"""

from olympus.api.errors import (
    ApiError,
    CursorAhead,
    InvalidCursor,
    RecoveryBlocked,
    RunNotFound,
    install_error_handlers,
)

__all__ = [
    "ApiError",
    "CursorAhead",
    "InvalidCursor",
    "RecoveryBlocked",
    "RunNotFound",
    "install_error_handlers",
]

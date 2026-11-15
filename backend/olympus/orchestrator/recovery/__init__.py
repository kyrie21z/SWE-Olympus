"""运行恢复：从持久化事件与快照重建内存运行态。

对应 issue #9 的第一条工作包：复用既有账本、许可与查询协议，
只对一种本地存储做恢复，不重复实现底层调用或租约系统。
"""

from .errors import RecoveryError, RecoveryGap
from .policy import BoundedRetryPolicy, RetryDecision
from .rebuild import rebuild_context, verify_chain
from .registry import RunRuntime, RuntimeRegistry

__all__ = [
    "BoundedRetryPolicy",
    "RecoveryError",
    "RecoveryGap",
    "RetryDecision",
    "RunRuntime",
    "RuntimeRegistry",
    "rebuild_context",
    "verify_chain",
]

"""状态机（OL-05）：Run 快照、受控迁移、合法边校验、改版失效。"""

from .revision import RevisionOutcome, affected_closure, invalidate_revision, supersede_plan
from .run_context import RunContext
from .transitions import ALLOWED, TERMINAL, IllegalTransition, assert_transition, can_transition

__all__ = [
    "ALLOWED",
    "TERMINAL",
    "IllegalTransition",
    "RevisionOutcome",
    "RunContext",
    "affected_closure",
    "assert_transition",
    "can_transition",
    "invalidate_revision",
    "supersede_plan",
]

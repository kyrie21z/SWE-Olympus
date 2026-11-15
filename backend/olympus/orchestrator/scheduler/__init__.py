"""Zeus 调度器（OL-05）。"""

from .executor import FakeAttempt, FakeExecutor
from .incidents import Incident, IncidentKind, IncidentLedger
from .scheduler import DEFAULT_EXECUTOR_CAPACITY, DEFAULT_MAX_PARALLEL, Permit, Scheduler

__all__ = [
    "DEFAULT_EXECUTOR_CAPACITY",
    "DEFAULT_MAX_PARALLEL",
    "FakeAttempt",
    "FakeExecutor",
    "Incident",
    "IncidentKind",
    "IncidentLedger",
    "Permit",
    "Scheduler",
]

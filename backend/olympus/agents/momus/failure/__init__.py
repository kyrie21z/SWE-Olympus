"""失败归因的离线实现（issue #9 第 2 条工作包的一半）。"""

from .attribution import (
    FailureAttribution,
    FailureEvidence,
    HeuristicFailureAttributor,
)

__all__ = ["FailureAttribution", "FailureEvidence", "HeuristicFailureAttributor"]

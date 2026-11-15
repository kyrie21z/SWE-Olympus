"""Server-side admission of upstream requirements; browser preflight is not authority."""

from .service import Admission, RequirementBridge, RequirementRejected

__all__ = ["Admission", "RequirementBridge", "RequirementRejected"]

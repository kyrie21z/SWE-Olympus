from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import threading
from typing import Callable, Mapping

from olympus.contracts.models import Confirmation, Ref, Requirement


class RequirementRejected(ValueError):
    """An import is not an authorized, current upstream requirement."""


@dataclass(frozen=True)
class Admission:
    requirement: Requirement
    synthetic: bool
    confirmation_id: str


class RequirementBridge:
    """Authority comes from trusted server configuration, never the uploaded actor string.

    The host supplies verified confirmation records, source IDs and dependency refs.
    This adapter checks those records; it does not invent signatures or identity services.
    Synthetic admission is an explicit classroom/test option, never real human approval.
    """

    def __init__(
        self, *, authorized_actors: set[str] | None = None,
        trusted_confirmations: Mapping[str, Confirmation] | None = None,
        trusted_sources: set[str] | None = None,
        current_dependencies: list[Ref] | None = None,
        allow_synthetic: bool = False,
        clock: Callable[[], datetime] | None = None,
    ):
        self.actors = frozenset(authorized_actors or ())
        self.confirmations = dict(trusted_confirmations or {})
        self.sources = frozenset(trusted_sources or ())
        self.dependencies = list(current_dependencies or ())
        self.allow_synthetic = allow_synthetic
        self.clock = clock or (lambda: datetime.now(UTC))
        self._current: dict[str, Admission] = {}
        self._lock = threading.RLock()

    def admit(self, raw: str | dict | Requirement) -> Admission:
        with self._lock:
            return self._admit(raw)

    def _admit(self, raw: str | dict | Requirement) -> Admission:
        # Revalidate even a caller's model_copy(update=...) object.
        if isinstance(raw, Requirement):
            req = Requirement.model_validate(raw.model_dump(mode="json"))
        elif isinstance(raw, str):
            req = Requirement.model_validate_json(raw)
        else:
            req = Requirement.model_validate(raw)
        confirmation = req.confirmation
        if confirmation is None or confirmation.target != req.ref():
            raise RequirementRejected("missing or stale requirement confirmation")
        if confirmation.confirmed_at > self.clock():
            raise RequirementRejected("confirmation is dated in the future")
        synthetic = confirmation.origin == "synthetic_fixture" or any(
            p.kind == "synthetic_fixture" for p in req.provenance
        )
        if synthetic:
            if not self.allow_synthetic:
                raise RequirementRejected("synthetic input requires explicit test admission")
        else:
            if confirmation.actor not in self.actors:
                raise RequirementRejected("confirmation actor is not authorized")
            if self.confirmations.get(confirmation.confirmation_id) != confirmation:
                raise RequirementRejected("confirmation is not in the trusted upstream registry")
            if any(p.kind not in {"human", "material"} or p.source_id not in self.sources
                   for p in req.provenance):
                raise RequirementRejected("unverified upstream provenance")
        if any(ref not in self.dependencies for ref in confirmation.dependencies):
            raise RequirementRejected("confirmation dependency is stale or unavailable")
        previous = self._current.get(req.object_id)
        if previous and (
            req.version < previous.requirement.version or
            (req.version == previous.requirement.version and req.ref() != previous.requirement.ref())
        ):
            raise RequirementRejected("requirement version rollback or same-version content conflict")
        admission = Admission(req, synthetic, confirmation.confirmation_id)
        self._current[req.object_id] = Admission(req.model_copy(deep=True), synthetic, confirmation.confirmation_id)
        return admission

    def require_current(self, req: Requirement) -> Admission:
        with self._lock:
            current = self._current.get(req.object_id)
            if current is None or current.requirement.ref() != req.ref():
                raise RequirementRejected("requirement is not the current admitted baseline")
            return self._admit(req)

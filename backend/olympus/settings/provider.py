"""Route each role to its own model while keeping one shared runner ledger."""

from __future__ import annotations

from pydantic import JsonValue

from olympus.agents.runtime import AgentRunner
from olympus.agents.runtime.models import ProviderNotConfiguredError, RoleRunInput
from olympus.agents.runtime.providers import ProviderStepResult
from olympus.settings.agents import AgentSettingsStore


class RoleProvider:
    mode = "real_model"

    def __init__(self, settings: AgentSettingsStore):
        self.settings = settings

    def steps(self, request: RoleRunInput) -> list[dict[str, JsonValue]]:
        # Bind the provider here, before the runner registers/sends a request. A
        # configuration edit during an in-flight call cannot change its target.
        try:
            provider = self.settings.get(request.role_id).provider()
        except ValueError as exc:
            raise ProviderNotConfiguredError("Unknown model agent") from exc
        return [{"kind": "model", "_role_provider": provider}]

    def invoke(self, request, step, *, step_index, prompt=None) -> ProviderStepResult:
        provider = step["_role_provider"]
        return provider.invoke(request, {"kind": "model"}, step_index=step_index, prompt=prompt)


def make_runner(settings: AgentSettingsStore, *, ledger=None, records=None) -> AgentRunner:
    return AgentRunner(RoleProvider(settings), ledger=ledger, records=records)

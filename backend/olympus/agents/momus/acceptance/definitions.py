"""Trusted acceptance definitions are frozen before implementation is dispatched."""

from pydantic import Field

from olympus.contracts.models import Contract, Hash, Id, Task, fingerprint


class FrozenAcceptance(Contract):
    checks: dict[Id, str] = Field(min_length=1)
    fixtures: dict = Field(default_factory=dict)
    content_hash: Hash
    author: Id = "Momus"

    @classmethod
    def freeze(cls, task: Task, checks: dict[str, str], fixtures: dict | None = None):
        if set(checks) != set(task.checks) or any(not text.strip() for text in checks.values()):
            raise ValueError("frozen scripts must cover exactly the approved task checks")
        body = {"checks": checks, "fixtures": fixtures or {}, "author": "Momus"}
        return cls(**body, content_hash=fingerprint(body))

    def verify(self, task: Task) -> None:
        body = self.model_dump(mode="json", exclude={"schema_version", "content_hash"})
        if (fingerprint(body) != self.content_hash or self.author != "Momus" or
                set(self.checks) != set(task.checks)):
            raise ValueError("acceptance definition changed after freezing")

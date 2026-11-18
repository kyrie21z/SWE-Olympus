"""Keep model credentials on the server, separate from public configuration views."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, field_validator

from olympus.agents.runtime.models import ProviderNotConfiguredError
from olympus.agents.runtime.providers import RealProvider

AGENT_ROLES = ("Athena", "Daedalus", "Momus", "Hephaestus", "Argus", "Hermes")


def check_role(role_id: str) -> str:
    if role_id not in AGENT_ROLES:
        raise ValueError("unknown model agent")
    return role_id


class AgentSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    base_url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=160)
    api_key: SecretStr | None = None
    timeout_seconds: int | None = Field(default=None, strict=True, ge=1, le=300)
    clear_api_key: StrictBool = False

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"} and parsed.hostname
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
                and parsed.port != 0
            )
        except ValueError:
            valid = False
        if not valid or any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError("base_url must be an HTTP(S) API root without credentials, query or fragment")
        return value

    @field_validator("model")
    @classmethod
    def valid_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(ord(char) < 32 for char in value):
            raise ValueError("model must not contain control characters")
        return value.strip()

    @field_validator("api_key")
    @classmethod
    def valid_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        if len(raw) > 8192 or any(ord(char) < 32 for char in raw):
            raise ValueError("api_key has an invalid format")
        return SecretStr(raw)


class AgentSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    role_id: str
    base_url: str = ""
    model: str = ""
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    timeout_seconds: int = 120

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key.get_secret_value())

    def public(self) -> dict:
        return {
            "role_id": self.role_id,
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "configured": self.configured,
            "has_api_key": bool(self.api_key.get_secret_value()),
        }

    def provider(self) -> RealProvider:
        if not self.configured:
            raise ProviderNotConfiguredError(f"Agent {self.role_id} is not configured")
        return RealProvider(
            api_key=self.api_key.get_secret_value(), base_url=self.base_url,
            model=self.model, timeout_seconds=self.timeout_seconds,
        )


class AgentSettingsStore:
    """Persist credentials only in the host's local SQLite database.

    The public API never serializes this table or the private settings model. Each
    update reads the previous secret in the same transaction, so a partial update
    cannot erase a concurrent credentials change.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS role_model_settings (
                    role_id TEXT PRIMARY KEY,
                    base_url TEXT NOT NULL,
                    model TEXT NOT NULL,
                    api_key TEXT NOT NULL,
                    timeout_seconds INTEGER NOT NULL
                )
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _read(db, role_id: str) -> AgentSettings:
        row = db.execute(
            "SELECT base_url,model,api_key,timeout_seconds FROM role_model_settings WHERE role_id=?",
            (role_id,),
        ).fetchone()
        if row is None:
            return AgentSettings(role_id=role_id)
        return AgentSettings(
            role_id=role_id, base_url=row[0], model=row[1], api_key=SecretStr(row[2]), timeout_seconds=row[3],
        )

    def get(self, role_id: str) -> AgentSettings:
        check_role(role_id)
        with self.connection() as db:
            return self._read(db, role_id)

    def public_list(self) -> list[dict]:
        with self.connection() as db:
            return [self._read(db, role_id).public() for role_id in AGENT_ROLES]

    def update(self, role_id: str, update: AgentSettingsUpdate) -> AgentSettings:
        check_role(role_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                current = self._read(db, role_id)
                key = current.api_key.get_secret_value()
                if update.clear_api_key:
                    key = ""
                elif update.api_key is not None and update.api_key.get_secret_value():
                    key = update.api_key.get_secret_value()
                result = AgentSettings(
                    role_id=role_id,
                    base_url=current.base_url if update.base_url is None else update.base_url,
                    model=current.model if update.model is None else update.model,
                    api_key=SecretStr(key),
                    timeout_seconds=(
                        current.timeout_seconds if update.timeout_seconds is None else update.timeout_seconds
                    ),
                )
                db.execute(
                    """INSERT INTO role_model_settings VALUES (?,?,?,?,?)
                       ON CONFLICT(role_id) DO UPDATE SET base_url=excluded.base_url,
                       model=excluded.model,api_key=excluded.api_key,timeout_seconds=excluded.timeout_seconds""",
                    (role_id, result.base_url, result.model, key, result.timeout_seconds),
                )
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise

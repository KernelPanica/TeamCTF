"""Versioned, bounded data crossing the Portal/Arena trust boundary."""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, AwareDatetime


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateMatch(WireModel):
    match_id: Annotated[int, Field(gt=0)]
    run_id: UUID
    case_id: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)]
    red_key: SecretStr = Field(min_length=1, max_length=4096)


class Endpoint(WireModel):
    name: str = Field(max_length=100)
    host: str = Field(max_length=253)
    port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"] = "tcp"


class ServiceHealth(WireModel):
    status: Literal["HEALTHY", "UNHEALTHY"]
    reason: Literal["OK", "CHECK_FAILED", "CHECK_ERROR", "TIMEOUT"]


class Observation(WireModel):
    health: dict[str, ServiceHealth] = Field(max_length=100)
    exploit: Literal["VULNERABLE", "PATCHED", "UNREACHABLE", "ERROR"]
    surface: Literal["AVAILABLE", "UNAVAILABLE"]
    red_exploit: Literal["VULNERABLE", "PATCHED", "UNREACHABLE", "ERROR"]
    red_services: dict[str, Literal["AVAILABLE", "UNAVAILABLE"]] = Field(max_length=100)
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)


class ArenaEvent(WireModel):
    sequence: int = Field(gt=0)
    timestamp: AwareDatetime
    observation: Observation


class ArenaStatus(WireModel):
    match_id: int = Field(gt=0)
    run_id: UUID
    instance_id: UUID
    state: Literal["PROVISIONING", "READY", "FAILED", "DELETING", "DELETED"]
    endpoints: list[Endpoint] = Field(default_factory=list, max_length=101)
    blue_password: SecretStr | None = None
    error: Literal["EXECUTION_FAILED", "CLEANUP_FAILED"] | None = None


class EventPage(WireModel):
    run_id: UUID
    instance_id: UUID
    events: list[ArenaEvent] = Field(max_length=100)
    cursor: int = Field(ge=0)


class ArenaError(RuntimeError):
    def __init__(self, code: str, status: int = 503):
        super().__init__(code)
        self.code, self.status = code, status


class ArenaProvider(ABC):
    @abstractmethod
    async def create_match(self, request: CreateMatch) -> ArenaStatus: ...

    @abstractmethod
    async def get_match(self, match_id: int) -> ArenaStatus: ...

    @abstractmethod
    async def get_events(self, match_id: int, after: int = 0) -> EventPage: ...

    @abstractmethod
    async def destroy_match(self, match_id: int) -> None: ...

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ShortText = Annotated[str, Field(min_length=1, max_length=512)]


class Alert(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: Literal["firing", "resolved"]
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime = Field(alias="startsAt")
    ends_at: datetime | None = Field(default=None, alias="endsAt")
    generator_url: str = Field(default="", alias="generatorURL", max_length=2_048)
    fingerprint: str = Field(min_length=1, max_length=256)


class AlertmanagerPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str = "4"
    status: Literal["firing", "resolved"]
    alerts: list[Alert] = Field(min_length=1, max_length=200)


class AlertResponse(BaseModel):
    accepted: int
    deduplicated: int
    cooldown: int
    budget_limited: int
    paused: int = 0
    ignored: int = 0
    pending: int = 0
    suppressed: int = 0


class AlertPolicyRequest(BaseModel):
    policy: Literal["auto", "manual", "ignore"]
    note: str = Field(default="", max_length=512)


class ApprovalDecisionRequest(BaseModel):
    action: Literal["approve", "reject", "rollback"]
    decided_by: ShortText
    note: str = Field(default="", max_length=2_000)


class ApprovalDecisionResponse(BaseModel):
    accepted: bool
    message: str


class ControlRequest(BaseModel):
    reason: str = Field(default="", max_length=512)


class TaskRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4_000)
    repo: str = Field(default="", max_length=1_024)
    project: str = Field(default="", max_length=128)


class TaskDispatchResponse(BaseModel):
    task_id: str
    message: str


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail

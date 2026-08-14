"""Pydantic models: the validation boundary.

Two families:

  * Event models  - what we accept from the feed. Deliberately permissive about
    nulls, because the real world sends them and dropping an event on a null
    forecast would lose information we can still partly use.

  * Rule models   - what we accept from users. Deliberately strict, because a
    rule that cannot fire is worse than no rule: the user believes they are
    covered and they are not.
"""

from datetime import datetime
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from . import catalog

# --------------------------------------------------------------------- events

AgentState = Literal["available", "on_call", "on_break", "in_meeting", "offline", "training"]


class BaseEvent(BaseModel):
    event_id: str
    ts: datetime
    type: str


class QueueSnapshot(BaseEvent):
    type: Literal["queue_snapshot"]
    queue_id: str
    tickets_waiting: Optional[int] = None
    longest_wait_sec: Optional[int] = None
    sla_target_sec: Optional[int] = None
    agents_available: Optional[int] = None
    agents_on_call: Optional[int] = None
    volume_last_15m: Optional[int] = None
    volume_forecast_next_15m: Optional[int] = None


class AgentStateChange(BaseEvent):
    type: Literal["agent_state_change"]
    agent_id: str
    # The feed sends both `null` and `[]` here. Normalise to a list so callers
    # never branch on which flavour of empty they got.
    queue_ids: list[str] = Field(default_factory=list)
    previous_state: Optional[str] = None
    previous_state_duration_sec: Optional[int] = None
    new_state: str

    @field_validator("queue_ids", mode="before")
    @classmethod
    def _null_queue_ids_are_empty(cls, value: Any) -> list[str]:
        return value or []


class AdherenceCheck(BaseEvent):
    type: Literal["adherence_check"]
    agent_id: str
    queue_ids: list[str] = Field(default_factory=list)
    scheduled_state: Optional[str] = None
    actual_state: Optional[str] = None
    in_violation: bool = False
    # May be null even when in_violation is true. We keep the contradiction
    # rather than inventing a start time; the engine skips duration rules and
    # records why.
    violation_started_at: Optional[datetime] = None

    @field_validator("queue_ids", mode="before")
    @classmethod
    def _null_queue_ids_are_empty(cls, value: Any) -> list[str]:
        return value or []


AnyEvent = Annotated[
    QueueSnapshot | AgentStateChange | AdherenceCheck,
    Field(discriminator="type"),
]


class EventEnvelope(BaseModel):
    """Wrapper so the ingest endpoint takes one event or a batch."""

    events: list[AnyEvent]


# ---------------------------------------------------------------------- rules

Operator = Literal["gt", "gte", "lt", "lte", "eq"]
ScopeType = Literal["all", "ids", "queues"]
Channel = Literal["slack", "email", "in_app"]
Severity = Literal["info", "warning", "critical"]


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    subject_type: Literal["queue", "agent"]
    metric: str
    operator: Operator
    threshold: float
    duration_sec: int = Field(default=0, ge=0, le=86_400)

    scope_type: ScopeType = "all"
    scope_ids: list[str] = Field(default_factory=list)

    recipient_id: str
    channel: Channel = "in_app"
    severity: Severity = "warning"

    cooldown_sec: int = Field(default=1800, ge=0, le=86_400)
    notify_on_resolve: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def _check_coherent(self) -> "RuleCreate":
        metric = catalog.get(self.metric)
        if metric is None:
            known = ", ".join(sorted(catalog.METRICS))
            raise ValueError(f"unknown metric '{self.metric}'. known metrics: {known}")

        # A queue metric on an agent rule can never match anything.
        if metric.subject_type != self.subject_type:
            raise ValueError(
                f"metric '{self.metric}' describes a {metric.subject_type}, "
                f"but this rule watches a {self.subject_type}"
            )

        # 'queues' scope narrows agents by the queues they serve. It is
        # meaningless for a rule whose subject is already a queue.
        if self.scope_type == "queues" and self.subject_type != "agent":
            raise ValueError("scope_type 'queues' only applies to agent rules")

        if self.scope_type in ("ids", "queues") and not self.scope_ids:
            raise ValueError(f"scope_type '{self.scope_type}' requires at least one id")

        if self.scope_type == "all" and self.scope_ids:
            raise ValueError("scope_type 'all' must not list ids")

        # Ratios are fractions of a target, not percentages. Catching this here
        # stops a user typing 80 (meaning 80%) and building a rule that can
        # never fire.
        if metric.unit == "ratio" and self.threshold > 10:
            raise ValueError(
                f"'{self.metric}' is a ratio: 1.0 means 100%. "
                f"Did you mean {self.threshold / 100} instead of {self.threshold}?"
            )

        if metric.unit in ("count", "seconds") and self.threshold < 0:
            raise ValueError("threshold cannot be negative for this metric")

        return self


class RuleUpdate(BaseModel):
    """Partial update. Only the noise and lifecycle knobs are editable in place;
    changing what a rule measures is a new rule, so history stays coherent."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    threshold: Optional[float] = None
    duration_sec: Optional[int] = Field(default=None, ge=0, le=86_400)
    cooldown_sec: Optional[int] = Field(default=None, ge=0, le=86_400)
    notify_on_resolve: Optional[bool] = None
    enabled: Optional[bool] = None
    severity: Optional[Severity] = None
    channel: Optional[Channel] = None


class RuleRead(BaseModel):
    id: int
    name: str
    enabled: bool
    subject_type: str
    metric: str
    operator: str
    threshold: float
    duration_sec: int
    scope_type: str
    scope_ids: list[str]
    recipient_id: str
    recipient_name: Optional[str] = None
    channel: str
    severity: str
    cooldown_sec: int
    notify_on_resolve: bool
    created_at: str
    # Rendered server-side so every surface says the same sentence.
    summary: str


class NotificationRead(BaseModel):
    id: int
    rule_id: int
    rule_name: str
    subject_id: str
    recipient_id: str
    channel: str
    severity: str
    kind: str
    title: str
    body: str
    value: Optional[float]
    event_ts: str
    created_at: str


class IngestResult(BaseModel):
    accepted: int
    duplicates: int
    stale: int
    notifications: int

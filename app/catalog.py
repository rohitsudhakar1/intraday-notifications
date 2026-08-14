"""The metric catalog.

A rule row stores `metric` as a plain string. This module is the lookup table
that says what that string means: which subject it describes, where its value
comes from, how to phrase it in a notification.

Adding a new rule type is adding a METRICS entry. The only case that needs new
Python is a genuinely new *derivation* (a value that is not a field and not a
ratio of two fields), and even then it is one small function, not a new branch
through the engine.

An extractor has three possible answers, and keeping them distinct is the
difference between a correct engine and a plausible one:

  a number        the metric measured cleanly
  None            "I do not know". The data needed is genuinely absent -- a
                  null forecast, a violation with no start time. We cannot say
                  the condition holds and we cannot say it broke, so the open
                  episode is left exactly as it was.
  NOT_APPLICABLE  "this does not apply to this subject right now". The agent is
                  not on a call, so call length is meaningless. That is not
                  ignorance, it is a definite negative: the condition does not
                  hold, and any open episode should close.

Collapsing those last two would leave a long-call episode open forever after
the agent hangs up, which silently blocks every future alert for that pair.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Union


class _NotApplicable:
    """Singleton sentinel. Distinct from None on purpose -- see module docstring."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "NOT_APPLICABLE"

    def __bool__(self) -> bool:
        return False


NOT_APPLICABLE = _NotApplicable()

Reading = Union[float, None, _NotApplicable]

# ---------------------------------------------------------------- extractors
# `snap` is the last known state dict for the subject. For queues that is the
# raw queue_snapshot payload. For agents it is the agent_states row, plus a
# "now" key holding the engine's current logical clock.

Extractor = Callable[[dict], Reading]


def field(name: str) -> Extractor:
    """Read a numeric field straight off the subject's last known state."""

    def extract(snap: dict) -> Reading:
        value = snap.get(name)
        return None if value is None else float(value)

    return extract


def ratio(numerator: str, denominator: str) -> Extractor:
    """numerator / denominator, guarding null and divide-by-zero."""

    def extract(snap: dict) -> Reading:
        num, den = snap.get(numerator), snap.get(denominator)
        if num is None or den is None or float(den) == 0.0:
            return None
        return float(num) / float(den)

    return extract


def seconds_since(field_name: str, requires: Optional[str] = None) -> Extractor:
    """How long ago `field_name` (an ISO timestamp) was, per the logical clock.

    This is what turns a point-in-time event into a duration.

    `requires` names a flag that must be truthy for the metric to mean anything.
    An agent who is in adherence has no violation to measure, which is
    NOT_APPLICABLE. An agent who IS in violation but whose start time is missing
    is None -- the duration is real but unknowable, and that is the contradiction
    the sample feed contains.
    """

    def extract(snap: dict) -> Reading:
        if requires is not None and not snap.get(requires):
            return NOT_APPLICABLE
        started, now = snap.get(field_name), snap.get("now")
        if not started:
            return None
        if not now:
            return None
        return (_parse(now) - _parse(started)).total_seconds()

    return extract


def seconds_in_state(*states: str) -> Extractor:
    """How long the agent has been in the current state, but only if that state
    is one of `states`.

    An agent on a break is not on a long call -- that is NOT_APPLICABLE, a
    definite no, which closes any open long-call episode. Returning None here
    instead would leave the episode open after the agent hangs up and block
    every future alert for that pair.
    """

    def extract(snap: dict) -> Reading:
        if snap.get("state") not in states:
            return NOT_APPLICABLE
        entered, now = snap.get("entered_at"), snap.get("now")
        if not entered or not now:
            return None
        return (_parse(now) - _parse(entered)).total_seconds()

    return extract


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ------------------------------------------------------------------- catalog


@dataclass(frozen=True)
class Metric:
    key: str
    label: str                # what the rule builder shows the user
    subject_type: str         # 'queue' | 'agent'
    unit: str                 # 'count' | 'seconds' | 'ratio'
    extract: Extractor
    # Phrasing for the notification body. {value} is formatted per unit,
    # {subject} is the queue_id or agent_id, {threshold} the configured limit.
    phrase: str
    # True when the value is itself a duration. Such metrics must be swept on a
    # timer, because no incoming event announces "still on the same call".
    time_driven: bool = False
    help_text: str = ""


METRICS: dict[str, Metric] = {}


def _register(metric: Metric) -> None:
    METRICS[metric.key] = metric


# ----- queue metrics
_register(Metric(
    key="tickets_waiting",
    label="Tickets waiting",
    subject_type="queue",
    unit="count",
    extract=field("tickets_waiting"),
    phrase="{subject} has {value} tickets waiting (limit {threshold})",
    help_text="Backlog depth right now.",
))

_register(Metric(
    key="longest_wait_sec",
    label="Longest wait",
    subject_type="queue",
    unit="seconds",
    extract=field("longest_wait_sec"),
    phrase="the oldest ticket in {subject} has waited {value} (limit {threshold})",
    help_text="How long the oldest unanswered ticket has been sitting.",
))

_register(Metric(
    key="sla_ratio",
    label="SLA consumed",
    subject_type="queue",
    unit="ratio",
    extract=ratio("longest_wait_sec", "sla_target_sec"),
    phrase="{subject} is at {value} of its SLA target (limit {threshold})",
    help_text=(
        "Oldest wait divided by the queue's SLA target. 1.0 means the SLA is "
        "exactly breached, 0.8 means at risk. Comparing the ratio instead of "
        "raw seconds lets one rule cover queues with different SLAs."
    ),
))

_register(Metric(
    key="agents_available",
    label="Agents available",
    subject_type="queue",
    unit="count",
    extract=field("agents_available"),
    phrase="{subject} has {value} agents available (limit {threshold})",
    help_text="Use with the 'below' operator to catch coverage gaps.",
))

_register(Metric(
    key="volume_vs_forecast",
    label="Volume vs forecast",
    subject_type="queue",
    unit="ratio",
    extract=ratio("volume_last_15m", "volume_forecast_next_15m"),
    phrase="{subject} volume is {value} of forecast (limit {threshold})",
    help_text=(
        "Last 15 minutes of actual volume divided by the forecast for the next "
        "15. Skipped when the feed has no forecast."
    ),
))

# ----- agent metrics
_register(Metric(
    key="time_on_call",
    label="Time on a single call",
    subject_type="agent",
    unit="seconds",
    extract=seconds_in_state("on_call"),
    phrase="{subject} has been on one call for {value} (limit {threshold})",
    time_driven=True,
    help_text="Measured from the state change into on_call. Resets on any transition.",
))

_register(Metric(
    key="time_in_state",
    label="Time in current state",
    subject_type="agent",
    unit="seconds",
    extract=seconds_in_state("on_call", "on_break", "in_meeting", "offline", "available"),
    phrase="{subject} has been in one state for {value} (limit {threshold})",
    time_driven=True,
    help_text="Any state, not just calls. Catches an agent stuck offline.",
))

_register(Metric(
    key="adherence_violation_sec",
    label="Time out of adherence",
    subject_type="agent",
    unit="seconds",
    extract=seconds_since("violation_started_at", requires="in_violation"),
    phrase="{subject} has been out of adherence for {value} (limit {threshold})",
    time_driven=True,
    help_text=(
        "Measured from when the violation began. Skipped if the feed reports a "
        "violation without a start time, since the duration is unknowable."
    ),
))


def get(metric_key: str) -> Optional[Metric]:
    return METRICS.get(metric_key)


def for_subject(subject_type: str) -> list[Metric]:
    return [m for m in METRICS.values() if m.subject_type == subject_type]


def time_driven_keys() -> set[str]:
    return {m.key for m in METRICS.values() if m.time_driven}


# ------------------------------------------------------------------ display

OPERATOR_WORDS = {
    "gt": "is above",
    "gte": "is at or above",
    "lt": "is below",
    "lte": "is at or below",
    "eq": "equals",
}


def format_value(value: Optional[float], unit: str) -> str:
    """Render a raw number the way a support lead would say it out loud."""
    if value is None:
        return "unknown"
    if unit == "seconds":
        return humanize_seconds(value)
    if unit == "ratio":
        return f"{value * 100:.0f}%"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"


def humanize_seconds(total: float) -> str:
    total = int(total)
    if total < 60:
        return f"{total}s"
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m" if seconds == 0 else f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"


def describe(metric_key: str, operator: str, threshold: float, duration_sec: int) -> str:
    """One-line human summary of a rule, used in the UI and in notifications."""
    metric = get(metric_key)
    if metric is None:
        return f"{metric_key} {operator} {threshold}"
    text = f"{metric.label} {OPERATOR_WORDS.get(operator, operator)} {format_value(threshold, metric.unit)}"
    if duration_sec > 0:
        text += f" for {humanize_seconds(duration_sec)}"
    return text

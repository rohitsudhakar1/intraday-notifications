"""Delivery channels.

Stubbed on purpose: the take-home asks for routing logic, not integrations.
What matters here is the seam. `deliver` looks up a channel by name and hands
it a rendered Notification. Swapping the Slack stub for a real Slack client is
one function, and nothing in the engine changes.

Every channel also writes to a log file so a run leaves an artifact you can
read after the fact.
"""

import os
from dataclasses import dataclass
from typing import Callable, Optional

LOG_PATH = os.environ.get(
    "INTRADAY_LOG", os.path.join(os.path.dirname(__file__), "..", "notifications.log")
)

SEVERITY_ICON = {"info": "·", "warning": "!", "critical": "!!"}
KIND_ICON = {"alert": "NEW", "reminder": "STILL", "resolved": "OK", "digest": "HELD"}


@dataclass
class Notification:
    id: int
    channel: str
    severity: str
    kind: str
    title: str
    body: str
    recipient_name: str
    recipient_handle: Optional[str]
    event_ts: str


def _render(notification: Notification) -> str:
    icon = SEVERITY_ICON.get(notification.severity, "·")
    kind = KIND_ICON.get(notification.kind, notification.kind.upper())
    target = notification.recipient_handle or notification.recipient_name
    return (
        f"[{notification.event_ts}] {icon} {kind:<5} "
        f"{notification.channel:>6} -> {target:<16} "
        f"{notification.title}: {notification.body}"
    )


def _emit(notification: Notification) -> None:
    line = _render(notification)
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def send_slack(notification: Notification) -> None:
    _emit(notification)


def send_email(notification: Notification) -> None:
    _emit(notification)


def send_in_app(notification: Notification) -> None:
    # Already persisted to the notifications table by the engine; the /api
    # feed and the UI read from there. Printing keeps the console demo honest.
    _emit(notification)


CHANNELS: dict[str, Callable[[Notification], None]] = {
    "slack": send_slack,
    "email": send_email,
    "in_app": send_in_app,
}


def deliver(notification: Notification) -> None:
    """Never let a broken channel break ingestion. A dropped notification is
    bad; a stalled event pipeline is worse."""
    channel = CHANNELS.get(notification.channel, send_in_app)
    try:
        channel(notification)
    except Exception as error:  # pragma: no cover - defensive
        print(f"[notifier] delivery failed for #{notification.id}: {error}", flush=True)

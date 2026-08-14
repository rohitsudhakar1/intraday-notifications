"""Test fixtures.

Every test drives the engine directly with hand-built events on an in-memory
database. No HTTP, no sleeping, no wall clock: the engine measures time from
event timestamps, so a test can advance an hour in a microsecond and assert
exactly what fired. That property is why the logical clock exists.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import db as db_module
from app.engine import Engine
from app.models import AdherenceCheck, AgentStateChange, QueueSnapshot

T0 = datetime(2026, 5, 26, 9, 0, 0, tzinfo=timezone.utc)


def at(minutes: float = 0, seconds: float = 0) -> datetime:
    return T0 + timedelta(minutes=minutes, seconds=seconds)


@pytest.fixture(autouse=True)
def isolated_notification_log(tmp_path, monkeypatch):
    """Send test deliveries to a throwaway file.

    Without this the suite appends to the same notifications.log the demo
    writes, so a reviewer who runs the tests before the replay opens a file of
    fixture output labelled "rule 1" and reasonably concludes something is
    broken. The log is a demo artefact; tests must not touch it.
    """
    from app import notifier

    monkeypatch.setattr(notifier, "LOG_PATH", str(tmp_path / "notifications.log"))


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(db_module.SCHEMA)
    connection.execute(
        "INSERT INTO users (id, name, role, agent_id, slack_handle, email)"
        " VALUES ('u_lead', 'Lead', 'lead', NULL, '@lead', 'lead@example.com')"
    )
    connection.execute(
        "INSERT INTO users (id, name, role, agent_id, slack_handle, email)"
        " VALUES ('u_agent', 'Agent', 'agent', 'a_19', '@agent', 'agent@example.com')"
    )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def engine(conn):
    return Engine(conn)


@pytest.fixture
def make_rule(conn):
    """Insert a rule and return its id. Defaults are the quiet ones: fire once
    per episode, no reminders, no resolve notice, so each test opts in to only
    the behaviour it is actually asserting."""
    counter = {"n": 0}

    def _make(**overrides) -> int:
        counter["n"] += 1
        rule = {
            "name": f"rule {counter['n']}",
            "enabled": 1,
            "subject_type": "queue",
            "metric": "tickets_waiting",
            "operator": "gt",
            "threshold": 10.0,
            "duration_sec": 0,
            "scope_type": "all",
            "scope_ids": [],
            "recipient_id": "u_lead",
            "channel": "in_app",
            "severity": "warning",
            "cooldown_sec": 0,
            "notify_on_resolve": 0,
        }
        rule.update(overrides)
        cursor = conn.execute(
            "INSERT INTO rules (name, enabled, subject_type, metric, operator, threshold,"
            " duration_sec, scope_type, scope_ids, recipient_id, channel, severity,"
            " cooldown_sec, notify_on_resolve, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-05-26T09:00:00Z')",
            (
                rule["name"], rule["enabled"], rule["subject_type"], rule["metric"],
                rule["operator"], rule["threshold"], rule["duration_sec"],
                rule["scope_type"], json.dumps(rule["scope_ids"]), rule["recipient_id"],
                rule["channel"], rule["severity"], rule["cooldown_sec"],
                int(rule["notify_on_resolve"]),
            ),
        )
        conn.commit()
        return cursor.lastrowid

    return _make


# ------------------------------------------------------------- event builders

_seq = {"n": 0}


def _next_id() -> str:
    _seq["n"] += 1
    return f"evt_test_{_seq['n']:04d}"


def snapshot(ts: datetime, queue_id: str = "billing", **fields) -> QueueSnapshot:
    payload = {
        "event_id": fields.pop("event_id", _next_id()),
        "ts": ts,
        "type": "queue_snapshot",
        "queue_id": queue_id,
        "tickets_waiting": 0,
        "longest_wait_sec": 0,
        "sla_target_sec": 120,
        "agents_available": 5,
        "agents_on_call": 0,
        "volume_last_15m": 10,
        "volume_forecast_next_15m": 20,
    }
    payload.update(fields)
    return QueueSnapshot(**payload)


def state_change(ts: datetime, agent_id: str = "a_19", new_state: str = "on_call",
                 **fields) -> AgentStateChange:
    payload = {
        "event_id": fields.pop("event_id", _next_id()),
        "ts": ts,
        "type": "agent_state_change",
        "agent_id": agent_id,
        "queue_ids": ["billing"],
        "previous_state": None,
        "previous_state_duration_sec": None,
        "new_state": new_state,
    }
    payload.update(fields)
    return AgentStateChange(**payload)


def adherence(ts: datetime, agent_id: str = "a_19", in_violation: bool = True,
              **fields) -> AdherenceCheck:
    payload = {
        "event_id": fields.pop("event_id", _next_id()),
        "ts": ts,
        "type": "adherence_check",
        "agent_id": agent_id,
        "queue_ids": ["billing"],
        "scheduled_state": "available",
        "actual_state": "on_break" if in_violation else "available",
        "in_violation": in_violation,
        "violation_started_at": fields.pop("violation_started_at", ts if in_violation else None),
    }
    payload.update(fields)
    return AdherenceCheck(**payload)


def sent(conn, **filters) -> list[sqlite3.Row]:
    sql = "SELECT * FROM notifications"
    params = []
    if filters:
        sql += " WHERE " + " AND ".join(f"{k} = ?" for k in filters)
        params = list(filters.values())
    return conn.execute(sql + " ORDER BY id", params).fetchall()


def suppressed(conn, reason: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM suppressions WHERE reason = ?", (reason,)
    ).fetchone()["n"]

"""SQLite connection + schema.

SQL is kept Postgres-compatible in spirit (no SQLite-only types beyond the
storage classes) so the same DDL is a short hop from the real thing.
"""

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("INTRADAY_DB", os.path.join(os.path.dirname(__file__), "..", "intraday.db"))

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- people
-- Two audiences: 'lead' (watches queues + their team) and 'agent'
-- (watches only themselves). agent_id links a user row to the agent_id
-- that appears in the event stream.
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('lead', 'agent')),
    agent_id      TEXT,
    slack_handle  TEXT,
    email         TEXT
);

-- ---------------------------------------------------------------- events
-- Append-only log of everything we ingested. event_id is the PRIMARY KEY,
-- which is what makes ingestion idempotent: a replayed event collides and
-- is rejected instead of being counted twice.
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    ts           TEXT NOT NULL,
    type         TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    payload      TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('processed', 'stale'))
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_subject ON events (subject_id, ts);

-- ---------------------------------------------------------------- rules
-- One flat predicate per row. Every rule in this domain reduces to:
--   "for <subject> in <scope>, when <metric> <operator> <threshold>
--    holds for <duration_sec>, tell <recipient> on <channel>."
-- `metric` is a key into the metric catalog (app/catalog.py), which is what
-- makes adding a rule type a catalog entry rather than a new code path.
CREATE TABLE IF NOT EXISTS rules (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    enabled           INTEGER NOT NULL DEFAULT 1,

    subject_type      TEXT    NOT NULL CHECK (subject_type IN ('queue', 'agent')),
    metric            TEXT    NOT NULL,
    operator          TEXT    NOT NULL CHECK (operator IN ('gt', 'gte', 'lt', 'lte', 'eq')),
    threshold         REAL    NOT NULL,
    duration_sec      INTEGER NOT NULL DEFAULT 0,

    -- scope_type 'all'    -> every subject of subject_type
    -- scope_type 'ids'    -> scope_ids holds queue_ids or agent_ids
    -- scope_type 'queues' -> agent rules limited to agents serving these queues
    scope_type        TEXT    NOT NULL CHECK (scope_type IN ('all', 'ids', 'queues')),
    scope_ids         TEXT    NOT NULL DEFAULT '[]',

    recipient_id      TEXT    NOT NULL REFERENCES users (id),
    channel           TEXT    NOT NULL CHECK (channel IN ('slack', 'email', 'in_app')),
    severity          TEXT    NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),

    -- noise control, per rule
    cooldown_sec      INTEGER NOT NULL DEFAULT 1800,
    notify_on_resolve INTEGER NOT NULL DEFAULT 0,

    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_lookup ON rules (enabled, subject_type, metric);

-- ------------------------------------------------------- condition state
-- The heart of duration tracking. One row per (rule, subject) pair.
--   true_since = when the predicate most recently became continuously true
--   fired_at   = when we last notified for THIS episode (NULL = not yet)
-- An "episode" starts when the predicate flips false -> true and ends when
-- it flips back. Firing is edge-triggered off the episode, not off events,
-- which is what stops a breaching queue alerting on every snapshot.
CREATE TABLE IF NOT EXISTS condition_states (
    rule_id     INTEGER NOT NULL REFERENCES rules (id) ON DELETE CASCADE,
    subject_id  TEXT    NOT NULL,
    is_open     INTEGER NOT NULL DEFAULT 0,
    true_since  TEXT,
    fired_at    TEXT,
    fire_count  INTEGER NOT NULL DEFAULT 0,
    last_value  REAL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (rule_id, subject_id)
);

-- --------------------------------------------------------- live world state
-- Latest known state per agent. Needed because "on a single call for 45
-- minutes" cannot be answered by any one event: we must remember when the
-- agent entered the state and measure against the clock.
CREATE TABLE IF NOT EXISTS agent_states (
    agent_id             TEXT PRIMARY KEY,
    state                TEXT NOT NULL,
    entered_at           TEXT NOT NULL,
    queue_ids            TEXT NOT NULL DEFAULT '[]',
    scheduled_state      TEXT,
    in_violation         INTEGER NOT NULL DEFAULT 0,
    violation_started_at TEXT,
    last_event_ts        TEXT NOT NULL
);

-- Latest snapshot per queue, kept whole so derived metrics (sla_ratio,
-- forecast overshoot) can be recomputed without re-reading the event log.
CREATE TABLE IF NOT EXISTS queue_states (
    queue_id      TEXT PRIMARY KEY,
    payload       TEXT NOT NULL,
    last_event_ts TEXT NOT NULL
);

-- -------------------------------------------------------- notifications
CREATE TABLE IF NOT EXISTS notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id      INTEGER NOT NULL REFERENCES rules (id) ON DELETE CASCADE,
    rule_name    TEXT    NOT NULL,
    subject_id   TEXT    NOT NULL,
    recipient_id TEXT    NOT NULL REFERENCES users (id),
    channel      TEXT    NOT NULL,
    severity     TEXT    NOT NULL,
    kind         TEXT    NOT NULL CHECK (kind IN ('alert', 'reminder', 'resolved', 'digest')),
    title        TEXT    NOT NULL,
    body         TEXT    NOT NULL,
    value        REAL,
    event_ts     TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_feed ON notifications (recipient_id, id DESC);

-- --------------------------------------------------------- suppressions
-- Every time a rule matched but we chose NOT to notify, and why. This is the
-- audit trail that proves the system is quiet on purpose rather than broken.
CREATE TABLE IF NOT EXISTS suppressions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id    INTEGER,
    subject_id TEXT,
    reason     TEXT NOT NULL,
    detail     TEXT,
    event_ts   TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suppressions_reason ON suppressions (reason, id DESC);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection):
    """Run a unit of work atomically. One event in = one transaction."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise

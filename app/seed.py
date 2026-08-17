"""Demo users and rules.

These are written the way a real team would set them up on their first day,
not the way that makes the demo loudest. Every rule here answers a question
someone actually asks during a shift.

Run:  python -m app.seed
"""

import json
from datetime import datetime, timezone

from . import db

# The feed identifies people as "a_11". Nobody on a support floor talks that
# way, so every agent in the sample data gets a name here and notifications
# render it instead. In a real deployment this table is the customer's roster,
# synced from their HR or workforce system.
#
# The id stays the id everywhere that matters -- episode keys, joins,
# de-duplication. Only the rendered message uses the name.
USERS = [
    ("u_priya",  "Priya Raman",   "lead",  None,   "@priya",  "priya@example.com"),
    ("u_marcus", "Marcus Hale",   "lead",  None,   "@marcus", "marcus@example.com"),
    ("u_a19",    "Jordan Reyes",  "agent", "a_19", "@jordan", "jordan@example.com"),
    ("u_a88",    "Sam Okafor",    "agent", "a_88", "@sam",    "sam@example.com"),
    # The rest of the floor. No rules of their own yet, but their leads watch
    # them, and their names should appear when they do.
    ("u_a05",    "Ana Cruz",      "agent", "a_05", "@ana",    "ana@example.com"),
    ("u_a07",    "Dev Patel",     "agent", "a_07", "@dev",    "dev@example.com"),
    ("u_a11",    "Alex Chen",     "agent", "a_11", "@alex",   "alex@example.com"),
    ("u_a23",    "Nina Sokolov",  "agent", "a_23", "@nina",   "nina@example.com"),
    ("u_a31",    "Tom Bergeron",  "agent", "a_31", "@tom",    "tom@example.com"),
    ("u_a42",    "Riley Osei",    "agent", "a_42", "@riley",  "riley@example.com"),
]

# name, subject, metric, op, threshold, duration, scope_type, scope_ids,
# recipient, channel, severity, cooldown, notify_on_resolve
RULES = [
    # --- the lead's core question: are we breaking promises to customers?
    ("SLA breached",
     "queue", "sla_ratio", "gt", 1.0, 120, "all", [],
     "u_priya", "slack", "critical", 1800, True),

    # Same metric, lower bar, lower severity. Deliberately overlapping: the
    # at-risk warning arrives first and the breach escalates. Severity budgets
    # are what stop the pair becoming noise.
    ("SLA at risk",
     "queue", "sla_ratio", "gte", 0.8, 60, "all", [],
     "u_priya", "slack", "warning", 3600, False),

    # --- backlog, phrased the way the prompt phrases it
    ("Billing backlog over 15",
     "queue", "tickets_waiting", "gt", 15.0, 300, "ids", ["billing"],
     "u_priya", "slack", "warning", 1800, True),

    # --- coverage gap. Single predicate, so it says "nobody free" rather than
    # "nobody free AND tickets waiting"; sustaining it for 5 minutes is what
    # keeps it meaningful without needing a compound condition.
    ("No one available on billing",
     "queue", "agents_available", "lt", 1.0, 300, "ids", ["billing"],
     "u_priya", "slack", "warning", 1800, True),

    # --- "ping me if any of my agents has been on a single call over 45 min"
    ("Agent stuck on a long call",
     "agent", "time_on_call", "gt", 2700.0, 0, "queues", ["billing", "tier_2", "vip"],
     "u_priya", "slack", "warning", 1800, False),

    # --- "notify me when I've been out of adherence for more than ten minutes"
    # The agent gets their own nudge before their lead is told.
    ("You've drifted out of adherence",
     "agent", "adherence_violation_sec", "gt", 600.0, 0, "ids", ["a_19"],
     "u_a19", "slack", "info", 900, True),

    ("You've drifted out of adherence",
     "agent", "adherence_violation_sec", "gt", 600.0, 0, "ids", ["a_88"],
     "u_a88", "slack", "info", 900, True),

    # --- the lead's version of the same thing, with a longer fuse so the agent
    # has a chance to fix it themselves first.
    ("Tier 2 agent out of adherence",
     "agent", "adherence_violation_sec", "gt", 1200.0, 0, "queues", ["tier_2"],
     "u_marcus", "email", "warning", 1800, False),

    # --- early warning that we are about to out-run the plan. The threshold is
    # below 1.0 on purpose: by the time actual volume passes forecast the
    # backlog is already building, so the useful signal is "closing in on it".
    ("Volume closing in on forecast",
     "queue", "volume_vs_forecast", "gte", 0.85, 0, "all", [],
     "u_marcus", "in_app", "info", 3600, False),
]


def seed(conn) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    for user in USERS:
        conn.execute(
            "INSERT INTO users (id, name, role, agent_id, slack_handle, email)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
            user,
        )

    if conn.execute("SELECT COUNT(*) AS n FROM rules").fetchone()["n"] == 0:
        for rule in RULES:
            (name, subject, metric, operator, threshold, duration,
             scope_type, scope_ids, recipient, channel, severity,
             cooldown, on_resolve) = rule
            conn.execute(
                "INSERT INTO rules (name, enabled, subject_type, metric, operator,"
                " threshold, duration_sec, scope_type, scope_ids, recipient_id,"
                " channel, severity, cooldown_sec, notify_on_resolve, created_at)"
                " VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, subject, metric, operator, threshold, duration, scope_type,
                 json.dumps(scope_ids), recipient, channel, severity, cooldown,
                 int(on_resolve), now),
            )
    conn.commit()


if __name__ == "__main__":
    connection = db.connect()
    db.init_db(connection)
    seed(connection)
    counts = {
        table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in ("users", "rules")
    }
    print(f"seeded: {counts['users']} users, {counts['rules']} rules")

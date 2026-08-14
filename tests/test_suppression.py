"""Noise control.

This is the file that matters most. A notification system that fires correctly
but too often is useless -- people mute it, and then it is worse than nothing
because everyone believes they are covered. Every test here is a promise about
silence.
"""

from tests.conftest import adherence, at, sent, snapshot, state_change, suppressed


def test_a_breaching_queue_alerts_once_not_once_per_snapshot(engine, conn, make_rule):
    """The headline promise. Billing breaches and stays broken for an hour
    while snapshots keep arriving every 30 seconds. That is 120 chances to
    alert, and the right answer is one."""
    make_rule(metric="tickets_waiting", operator="gt", threshold=10.0, cooldown_sec=0)

    for tick in range(120):
        engine.ingest(snapshot(at(seconds=30 * tick), tickets_waiting=25))

    assert len(sent(conn)) == 1
    # ...and the other 119 are accounted for, not lost.
    assert suppressed(conn, "already_open") == 119


def test_recovery_then_relapse_is_a_second_incident(engine, conn, make_rule):
    """De-duplication must not become deafness. Two separate incidents deserve
    two alerts, and the way we tell them apart is the gap in between."""
    make_rule(metric="tickets_waiting", operator="gt", threshold=10.0, cooldown_sec=0)

    engine.ingest(snapshot(at(0), tickets_waiting=25))    # incident one
    engine.ingest(snapshot(at(5), tickets_waiting=2))     # recovered
    engine.ingest(snapshot(at(10), tickets_waiting=25))   # incident two

    assert len(sent(conn)) == 2


def test_cooldown_turns_a_long_incident_into_a_reminder(engine, conn, make_rule):
    """An hour-long outage should not be silent after the first minute. With a
    cooldown set, the follow-up arrives -- and it is labelled a reminder, so it
    does not read as the system double-firing."""
    make_rule(metric="tickets_waiting", operator="gt", threshold=10.0, cooldown_sec=1800)

    # 70 minutes of continuous breach, sampled every 5 minutes: 14 chances to
    # speak, a 30 minute cooldown, so exactly three messages.
    for minute in range(0, 70, 5):
        engine.ingest(snapshot(at(minute), tickets_waiting=25))

    notifications = sent(conn)
    assert [n["kind"] for n in notifications] == ["alert", "reminder", "reminder"]
    assert notifications[1]["title"].startswith("Still open")
    # The gap between messages is the cooldown, not the sampling rate.
    stamps = [n["event_ts"] for n in notifications]
    assert stamps == ["2026-05-26T09:00:00Z", "2026-05-26T09:30:00Z", "2026-05-26T10:00:00Z"]


def test_critical_gets_through_a_wall_of_warnings(engine, conn, make_rule):
    """The failure mode of a flat rate limit: routine traffic arrives first and
    the one message that mattered is the one that gets dropped.

    Twelve warning-severity incidents exhaust the warning budget. The critical
    that follows must still be delivered."""
    for index in range(13):
        make_rule(name=f"warn {index}", metric="tickets_waiting", operator="gt",
                  threshold=float(index), severity="warning", cooldown_sec=0)
    make_rule(name="the one that matters", metric="longest_wait_sec", operator="gt",
              threshold=100.0, severity="critical", cooldown_sec=0)

    engine.ingest(snapshot(at(0), tickets_waiting=50, longest_wait_sec=999))

    delivered = sent(conn)
    assert suppressed(conn, "rate_limit") >= 1, "the warning budget should have run out"
    assert any(n["severity"] == "critical" for n in delivered), \
        "a critical must never be crowded out by routine warnings"


def test_rate_limited_alerts_collapse_into_one_digest(engine, conn, make_rule):
    """Suppressed must never mean invisible. When the budget is spent the user
    still learns that something was held back, and how bad it was."""
    for index in range(20):
        make_rule(name=f"info {index}", metric="tickets_waiting", operator="gt",
                  threshold=float(index), severity="info", cooldown_sec=0)

    engine.ingest(snapshot(at(0), tickets_waiting=50))

    digests = sent(conn, kind="digest")
    assert len(digests) == 1, "the noise control must not itself become noise"
    assert "suppressed" in digests[0]["body"]


def test_digest_is_rewritten_in_place_not_repeated(engine, conn, make_rule):
    """A digest is a running tally, not an event. More suppressions update the
    count; they do not queue up another message."""
    for index in range(20):
        make_rule(name=f"info {index}", metric="tickets_waiting", operator="gt",
                  threshold=float(index), severity="info", cooldown_sec=0)

    engine.ingest(snapshot(at(0), tickets_waiting=50))
    first = sent(conn, kind="digest")[0]["value"]
    engine.ingest(snapshot(at(1), tickets_waiting=51))

    digests = sent(conn, kind="digest")
    assert len(digests) == 1
    assert digests[0]["value"] > first, "the tally should have grown"


def test_resolved_is_only_sent_if_we_raised_the_alarm(engine, conn, make_rule):
    """Telling someone a problem is over when they were never told it started
    is pure noise, and it is confusing noise."""
    make_rule(metric="tickets_waiting", operator="gt", threshold=10.0,
              duration_sec=600, notify_on_resolve=1)

    engine.ingest(snapshot(at(0), tickets_waiting=25))   # opens, below the window
    engine.ingest(snapshot(at(2), tickets_waiting=1))    # closes before it ever fired

    assert sent(conn) == []


def test_an_agent_is_nudged_before_their_lead_is_told(engine, conn, make_rule):
    """Routing, not just filtering. The agent gets a chance to self-correct on
    a short fuse; the lead is only pulled in if it persists."""
    make_rule(name="self nudge", subject_type="agent", metric="adherence_violation_sec",
              operator="gt", threshold=600.0, scope_type="ids", scope_ids=["a_19"],
              recipient_id="u_agent", cooldown_sec=0)
    make_rule(name="escalation", subject_type="agent", metric="adherence_violation_sec",
              operator="gt", threshold=1800.0, scope_type="queues", scope_ids=["billing"],
              recipient_id="u_lead", cooldown_sec=0)

    started = at(0)
    for minute in (0, 12, 20, 35):
        engine.ingest(adherence(at(minute), violation_started_at=started))

    agent_first = sent(conn, recipient_id="u_agent")[0]
    lead_first = sent(conn, recipient_id="u_lead")[0]
    assert agent_first["event_ts"] < lead_first["event_ts"]


def test_a_breach_supersedes_the_at_risk_warning_for_the_same_queue(engine, conn, make_rule):
    """Warn at 80% of SLA, escalate at 100%. A queue that jumps straight past
    both should produce one message, not two descriptions of one situation."""
    make_rule(name="SLA breached", metric="sla_ratio", operator="gt", threshold=1.0,
              severity="critical", cooldown_sec=0)
    make_rule(name="SLA at risk", metric="sla_ratio", operator="gte", threshold=0.8,
              severity="warning", cooldown_sec=0)

    engine.ingest(snapshot(at(0), longest_wait_sec=300, sla_target_sec=120))  # 250%

    delivered = sent(conn)
    assert [n["rule_name"] for n in delivered] == ["SLA breached"]
    assert suppressed(conn, "superseded") == 1


def test_the_warning_returns_once_the_breach_clears(engine, conn, make_rule):
    """Supersession must not become permanent silence. When the queue recovers
    to merely at-risk, that is news again."""
    make_rule(name="SLA breached", metric="sla_ratio", operator="gt", threshold=1.0,
              severity="critical", cooldown_sec=0)
    make_rule(name="SLA at risk", metric="sla_ratio", operator="gte", threshold=0.8,
              severity="warning", cooldown_sec=0)

    engine.ingest(snapshot(at(0), longest_wait_sec=300, sla_target_sec=120))   # 250%, breached
    engine.ingest(snapshot(at(5), longest_wait_sec=108, sla_target_sec=120))   # 90%, at risk only

    assert [n["rule_name"] for n in sent(conn)] == ["SLA breached", "SLA at risk"]


def test_supersession_never_crosses_recipients(engine, conn, make_rule):
    """Silencing one person because a different person was told is never
    right, however severe the other alert was."""
    make_rule(name="lead critical", metric="sla_ratio", operator="gt", threshold=1.0,
              severity="critical", recipient_id="u_lead", cooldown_sec=0)
    make_rule(name="agent warning", metric="sla_ratio", operator="gt", threshold=1.0,
              severity="warning", recipient_id="u_agent", cooldown_sec=0)

    engine.ingest(snapshot(at(0), longest_wait_sec=300, sla_target_sec=120))

    assert len(sent(conn, recipient_id="u_lead")) == 1
    assert len(sent(conn, recipient_id="u_agent")) == 1

"""Duration tracking.

"For more than ten minutes" is the phrase that appears in every example in the
brief, and it is the part of the system that cannot be derived from a single
event. These tests pin down what "continuously" means at the edges.
"""

from tests.conftest import adherence, at, sent, snapshot, state_change, suppressed


def test_nothing_fires_before_the_window_closes(engine, conn, make_rule):
    make_rule(metric="tickets_waiting", operator="gt", threshold=10.0, duration_sec=600)

    for minute in range(0, 10):
        engine.ingest(snapshot(at(minute), tickets_waiting=25))

    assert sent(conn) == []
    assert suppressed(conn, "duration_not_met") == 10


def test_it_fires_once_the_window_closes(engine, conn, make_rule):
    make_rule(metric="tickets_waiting", operator="gt", threshold=10.0, duration_sec=600)

    for minute in range(0, 12):
        engine.ingest(snapshot(at(minute), tickets_waiting=25))

    notifications = sent(conn)
    assert len(notifications) == 1
    assert notifications[0]["event_ts"] == "2026-05-26T09:10:00Z"


def test_a_dip_below_threshold_restarts_the_clock(engine, conn, make_rule):
    """The heart of it. "Continuously for ten minutes" means one unbroken run.
    Nine minutes of breach, one minute of calm, then nine more is NOT eighteen
    minutes -- and a system that adds them up cries wolf."""
    make_rule(metric="tickets_waiting", operator="gt", threshold=10.0, duration_sec=600)

    for minute in range(0, 9):
        engine.ingest(snapshot(at(minute), tickets_waiting=25))
    engine.ingest(snapshot(at(9), tickets_waiting=1))       # the dip
    for minute in range(10, 19):
        engine.ingest(snapshot(at(minute), tickets_waiting=25))

    assert sent(conn) == []


def test_a_long_call_fires_with_no_event_to_announce_it(engine, conn, make_rule):
    """The case a purely event-driven engine gets wrong.

    The agent picks up at 09:00 and the feed says nothing more about them. At
    09:46 they are twelve minutes past a 45-minute limit. Only the timer sweep
    can notice, and it is driven by unrelated traffic elsewhere in the system.
    """
    make_rule(subject_type="agent", metric="time_on_call", operator="gt",
              threshold=2700.0, scope_type="all", scope_ids=[])

    engine.ingest(state_change(at(0), new_state="on_call"))
    # Unrelated queue traffic. It advances the logical clock, nothing more.
    for minute in range(1, 50):
        engine.ingest(snapshot(at(minute), queue_id="tier_2", tickets_waiting=0))

    notifications = sent(conn, subject_id="a_19")
    assert len(notifications) == 1
    assert notifications[0]["event_ts"] == "2026-05-26T09:46:00Z"


def test_duration_rules_fire_within_the_sweep_interval(engine, conn, make_rule):
    """The stated guarantee: a duration rule fires within 60s of its threshold,
    even when the subject's own feed is slower than that.

    Billing loses coverage at 09:00 with a five minute window, so it qualifies
    at 09:05 -- but billing snapshots only arrive every ten minutes."""
    make_rule(metric="agents_available", operator="lt", threshold=1.0, duration_sec=300)

    engine.ingest(snapshot(at(0), agents_available=0))
    for minute in range(1, 12):
        engine.ingest(snapshot(at(minute), queue_id="vip", tickets_waiting=0))

    notifications = sent(conn, subject_id="billing")
    assert len(notifications) == 1
    assert notifications[0]["event_ts"] == "2026-05-26T09:05:00Z"


def test_a_repeated_state_does_not_reset_the_timer(engine, conn, make_rule):
    """A chatty upstream that re-sends "still on_call" must not starve a
    long-call rule forever by restarting its clock every time."""
    make_rule(subject_type="agent", metric="time_on_call", operator="gt",
              threshold=2700.0, scope_type="all", scope_ids=[])

    for minute in range(0, 50):
        engine.ingest(state_change(at(minute), new_state="on_call"))

    assert len(sent(conn, subject_id="a_19")) == 1


def test_hanging_up_closes_the_episode(engine, conn, make_rule):
    """When the metric stops applying at all, that is a definite no, not a
    shrug. If it were treated as unknown the episode would stay open forever
    and silently block every future alert for this agent."""
    make_rule(subject_type="agent", metric="time_on_call", operator="gt",
              threshold=2700.0, scope_type="all", scope_ids=[], notify_on_resolve=1)

    engine.ingest(state_change(at(0), new_state="on_call"))
    for minute in range(1, 50):
        engine.ingest(snapshot(at(minute), queue_id="tier_2"))
    engine.ingest(state_change(at(50), new_state="available"))

    kinds = [n["kind"] for n in sent(conn, subject_id="a_19")]
    assert kinds == ["alert", "resolved"]

    state = conn.execute(
        "SELECT is_open FROM condition_states WHERE subject_id = 'a_19'"
    ).fetchone()
    assert state["is_open"] == 0, "the episode must not stay open after the call ends"


def test_a_new_call_after_a_long_one_starts_a_fresh_clock(engine, conn, make_rule):
    """Two 30-minute calls back to back are not one 60-minute call."""
    make_rule(subject_type="agent", metric="time_on_call", operator="gt",
              threshold=2700.0, scope_type="all", scope_ids=[])

    engine.ingest(state_change(at(0), new_state="on_call"))
    for minute in range(1, 30):
        engine.ingest(snapshot(at(minute), queue_id="tier_2"))
    engine.ingest(state_change(at(30), new_state="available"))
    engine.ingest(state_change(at(31), new_state="on_call"))
    for minute in range(32, 60):
        engine.ingest(snapshot(at(minute), queue_id="tier_2"))

    assert sent(conn, subject_id="a_19") == []

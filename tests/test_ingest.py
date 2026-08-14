"""Ingestion under real-world conditions.

Every case here is taken from the sample feed rather than imagined. The feed
ships a duplicate event_id, two events that arrive after newer ones, a null
queue list, a null forecast, and a violation flagged true with no start time.
A system that only works on clean input does not work.
"""

from tests.conftest import adherence, at, sent, snapshot, state_change, suppressed


def test_a_replayed_event_is_ignored(engine, conn, make_rule):
    """The feed contains evt_01HXYZ050 twice. Counting it twice would not just
    double a notification, it could reopen a closed episode and corrupt the
    duration state that every other rule depends on."""
    make_rule(metric="tickets_waiting", operator="gt", threshold=10.0)

    first = engine.ingest(snapshot(at(0), tickets_waiting=25, event_id="evt_dupe"))
    second = engine.ingest(snapshot(at(1), tickets_waiting=25, event_id="evt_dupe"))

    assert first["status"] == "processed"
    assert second["status"] == "duplicate"
    assert len(sent(conn)) == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 1


def test_a_late_event_does_not_rewrite_history(engine, conn, make_rule):
    """The last two lines of the feed are timestamped an hour earlier than the
    lines above them. Applying them over newer state would resurrect a problem
    that has already been resolved."""
    make_rule(metric="tickets_waiting", operator="gt", threshold=10.0)

    engine.ingest(snapshot(at(0), tickets_waiting=2))
    engine.ingest(snapshot(at(60), tickets_waiting=2))
    late = engine.ingest(snapshot(at(30), tickets_waiting=99))   # arrives out of order

    assert late["status"] == "stale"
    assert sent(conn) == [], "a stale event must not raise an alarm"
    assert suppressed(conn, "stale_event") == 1
    # Kept, because it is real data, just not allowed to drive state.
    row = conn.execute("SELECT status FROM events WHERE ts LIKE '%09:30%'").fetchone()
    assert row["status"] == "stale"


def test_a_null_queue_list_does_not_orphan_an_agent(engine, conn, make_rule):
    """Event 73 sends queue_ids: null for a_05. Overwriting the known queues
    with nothing would silently drop that agent out of every scoped rule --
    the worst kind of bug, because it looks like quiet."""
    make_rule(subject_type="agent", metric="time_on_call", operator="gt",
              threshold=60.0, scope_type="queues", scope_ids=["billing"])

    engine.ingest(state_change(at(0), new_state="available", queue_ids=["billing"]))
    engine.ingest(state_change(at(1), new_state="on_call", queue_ids=None))
    for minute in range(2, 6):
        engine.ingest(snapshot(at(minute), queue_id="tier_2"))

    assert len(sent(conn, subject_id="a_19")) == 1


def test_a_missing_forecast_skips_the_rule_rather_than_guessing(engine, conn, make_rule):
    """Event 68 has a null forecast. Treating null as zero makes the ratio
    infinite and alerts on nothing at all."""
    make_rule(metric="volume_vs_forecast", operator="gt", threshold=1.5)

    engine.ingest(snapshot(at(0), volume_last_15m=30, volume_forecast_next_15m=None))

    assert sent(conn) == []
    assert suppressed(conn, "missing_data") == 1


def test_a_violation_without_a_start_time_is_unknowable_not_zero(engine, conn, make_rule):
    """Event 86 reports a_23 in violation with violation_started_at: null. The
    duration is real but we cannot measure it, so we say so instead of
    pretending it just started."""
    make_rule(subject_type="agent", metric="adherence_violation_sec", operator="gt",
              threshold=600.0, scope_type="all", scope_ids=[])

    engine.ingest(adherence(at(0), in_violation=True, violation_started_at=None))

    assert sent(conn) == []
    assert suppressed(conn, "missing_data") == 1


def test_an_agent_back_in_adherence_closes_the_episode(engine, conn, make_rule):
    """Coming back into adherence is a definite recovery, not missing data."""
    make_rule(subject_type="agent", metric="adherence_violation_sec", operator="gt",
              threshold=600.0, scope_type="all", scope_ids=[], notify_on_resolve=1)

    started = at(0)
    engine.ingest(adherence(at(15), violation_started_at=started))
    engine.ingest(adherence(at(20), in_violation=False))

    assert [n["kind"] for n in sent(conn, subject_id="a_19")] == ["alert", "resolved"]


def test_ingestion_is_unaffected_by_a_broken_channel(engine, conn, make_rule, monkeypatch):
    """A dropped notification is bad. A stalled event pipeline is worse, because
    it takes every other rule down with it."""
    from app import notifier

    def explode(_notification):
        raise RuntimeError("slack is down")

    monkeypatch.setitem(notifier.CHANNELS, "in_app", explode)
    make_rule(metric="tickets_waiting", operator="gt", threshold=10.0)

    result = engine.ingest(snapshot(at(0), tickets_waiting=25))

    assert result["status"] == "processed"
    # Still recorded, so it shows up in the feed even though the channel failed.
    assert len(sent(conn)) == 1

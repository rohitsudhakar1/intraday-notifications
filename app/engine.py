"""The rule engine.

One event in, zero or more notifications out. The flow is:

    ingest      dedupe by event_id, reject stale, write to the log
      |
    apply       fold the event into live world state (queue_states/agent_states)
      |
    evaluate    for each rule matching this subject, compute the metric and
                test the predicate
      |
    sustain     update the (rule, subject) episode: is it true, and since when
      |
    suppress    decide whether this episode is allowed to notify right now
      |
    deliver     render the message and hand it to a channel

The two parts worth reading closely are `sustain` (duration tracking) and
`suppress` (noise control). Everything else is plumbing.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from . import catalog
from .models import AdherenceCheck, AgentStateChange, AnyEvent, QueueSnapshot
from .notifier import Notification, deliver

# How often the timer sweep re-checks duration metrics. The sweep exists
# because nothing in the feed ever announces "the agent is STILL on that call",
# so a purely event-driven engine would never fire a long-call rule.
SWEEP_INTERVAL_SEC = 60

# Ceiling on notifications per recipient per hour: the last line of defence
# against a badly configured account burying its owner.
#
# The budgets are PER SEVERITY and independent, which is the whole point. A
# flat cap treats an adherence nudge and a VIP outage identically, so twelve
# nudges arriving first would silently drop the one notification that mattered
# -- the noise problem inverted. Separate budgets mean routine traffic can
# never crowd out a critical.
#
# Critical still has a ceiling rather than a bypass: episode de-duplication and
# cooldown already bound a single rule to ~2/hour, so reaching 40 means dozens
# of distinct critical conditions, which is a genuine outage and worth every
# message. But an unbounded channel is not something to ship.
SEVERITY_HOURLY_CAP = {"info": 6, "warning": 12, "critical": 40}

SEVERITY_RANK = {"info": 1, "warning": 2, "critical": 3}


class Engine:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        # Logical clock: the newest event timestamp we have seen. Everything
        # time-based is measured against this rather than wall-clock, so a
        # replay of yesterday's feed behaves exactly like it did live and
        # tests are deterministic.
        self.now: Optional[datetime] = None
        self._last_sweep: Optional[datetime] = None
        # (rule_id, subject_id) pairs already evaluated at the current clock
        # value. The sweep consults this so it never re-does work the arriving
        # event just did, which would double every audit row.
        self._evaluated: set[tuple[int, str]] = set()

    # ------------------------------------------------------------------ ingest

    def ingest(self, event: AnyEvent) -> dict:
        """Returns a small receipt describing what happened to this event."""
        subject_id = _subject_id(event)
        outcome = {"status": "processed", "notifications": 0}

        # ---- 1. idempotency. The PRIMARY KEY on event_id does the work; we
        # just have to notice the collision rather than crashing on it. This
        # matters more than it looks: a duplicate event would otherwise be
        # folded into world state twice and could reopen a closed episode.
        try:
            self.conn.execute(
                "INSERT INTO events (event_id, ts, type, subject_id, payload, received_at, status)"
                " VALUES (?, ?, ?, ?, ?, ?, 'processed')",
                (
                    event.event_id,
                    _iso(event.ts),
                    event.type,
                    subject_id,
                    event.model_dump_json(),
                    _iso(_utcnow()),
                ),
            )
        except sqlite3.IntegrityError:
            self._suppress(
                None, subject_id, "duplicate_event",
                f"{event.event_id} was already ingested; not re-applied to state",
                _iso(event.ts),
            )
            self.conn.commit()
            return {"status": "duplicate", "notifications": 0}

        # ---- 2. staleness. The feed can deliver out of order. Applying an old
        # event over newer world state would rewrite history: an agent who has
        # since moved on would look like they were still on that call. We keep
        # the event (it is real data) but do not let it drive state or rules.
        if self._is_stale(subject_id, event.ts):
            self.conn.execute(
                "UPDATE events SET status = 'stale' WHERE event_id = ?", (event.event_id,)
            )
            self._suppress(
                None, subject_id, "stale_event",
                f"{_iso(event.ts)} arrived after newer state", _iso(event.ts),
            )
            self.conn.commit()
            return {"status": "stale", "notifications": 0}

        # ---- 3. advance the logical clock, then apply and evaluate.
        if self.now is None or event.ts > self.now:
            self.now = event.ts
            self._evaluated.clear()

        self._apply(event)
        fired = self._evaluate_subject(_subject_type(event), subject_id, event.ts)

        # ---- 4. the timer sweep. Duration metrics for OTHER subjects may have
        # crossed their threshold while we were not looking.
        fired += self._maybe_sweep()

        self.conn.commit()
        outcome["notifications"] = fired
        return outcome

    def ingest_many(self, events: Iterable[AnyEvent]) -> dict:
        totals = {"accepted": 0, "duplicates": 0, "stale": 0, "notifications": 0}
        for event in events:
            result = self.ingest(event)
            if result["status"] == "duplicate":
                totals["duplicates"] += 1
            elif result["status"] == "stale":
                totals["stale"] += 1
            else:
                totals["accepted"] += 1
            totals["notifications"] += result["notifications"]
        return totals

    def _is_stale(self, subject_id: str, ts: datetime) -> bool:
        row = self.conn.execute(
            "SELECT last_event_ts FROM queue_states WHERE queue_id = ?"
            " UNION ALL SELECT last_event_ts FROM agent_states WHERE agent_id = ?",
            (subject_id, subject_id),
        ).fetchone()
        return row is not None and ts < _parse(row["last_event_ts"])

    # ------------------------------------------------------------------- apply

    def _apply(self, event: AnyEvent) -> None:
        """Fold the event into live world state."""
        if isinstance(event, QueueSnapshot):
            payload = event.model_dump(mode="json")
            self.conn.execute(
                "INSERT INTO queue_states (queue_id, payload, last_event_ts) VALUES (?, ?, ?)"
                " ON CONFLICT (queue_id) DO UPDATE SET payload = excluded.payload,"
                " last_event_ts = excluded.last_event_ts",
                (event.queue_id, json.dumps(payload), _iso(event.ts)),
            )

        elif isinstance(event, AgentStateChange):
            prior = self._agent_row(event.agent_id)
            # entered_at only moves when the state actually changes. A repeated
            # "still available" event must not reset a duration timer, or a
            # long-call rule could be starved forever by chatty upstreams.
            entered_at = (
                prior["entered_at"]
                if prior and prior["state"] == event.new_state
                else _iso(event.ts)
            )
            # queue_ids can be null on a transition; keep the last known set
            # rather than orphaning the agent out of every scoped rule.
            queue_ids = event.queue_ids or (json.loads(prior["queue_ids"]) if prior else [])
            self.conn.execute(
                "INSERT INTO agent_states"
                " (agent_id, state, entered_at, queue_ids, scheduled_state,"
                "  in_violation, violation_started_at, last_event_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (agent_id) DO UPDATE SET state = excluded.state,"
                "  entered_at = excluded.entered_at, queue_ids = excluded.queue_ids,"
                "  last_event_ts = excluded.last_event_ts",
                (
                    event.agent_id, event.new_state, entered_at, json.dumps(queue_ids),
                    prior["scheduled_state"] if prior else None,
                    prior["in_violation"] if prior else 0,
                    prior["violation_started_at"] if prior else None,
                    _iso(event.ts),
                ),
            )

        elif isinstance(event, AdherenceCheck):
            prior = self._agent_row(event.agent_id)
            queue_ids = event.queue_ids or (json.loads(prior["queue_ids"]) if prior else [])
            state = event.actual_state or (prior["state"] if prior else "unknown")
            entered_at = (
                prior["entered_at"]
                if prior and prior["state"] == state
                else _iso(event.ts)
            )
            self.conn.execute(
                "INSERT INTO agent_states"
                " (agent_id, state, entered_at, queue_ids, scheduled_state,"
                "  in_violation, violation_started_at, last_event_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (agent_id) DO UPDATE SET"
                "  queue_ids = excluded.queue_ids,"
                "  scheduled_state = excluded.scheduled_state,"
                "  in_violation = excluded.in_violation,"
                "  violation_started_at = excluded.violation_started_at,"
                "  last_event_ts = excluded.last_event_ts",
                (
                    event.agent_id, state, entered_at, json.dumps(queue_ids),
                    event.scheduled_state,
                    1 if event.in_violation else 0,
                    _iso(event.violation_started_at) if event.violation_started_at else None,
                    _iso(event.ts),
                ),
            )

    def _agent_row(self, agent_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM agent_states WHERE agent_id = ?", (agent_id,)
        ).fetchone()

    # ---------------------------------------------------------------- evaluate

    def _evaluate_subject(self, subject_type: str, subject_id: str, ts: datetime) -> int:
        snapshot = self._snapshot(subject_type, subject_id)
        if snapshot is None:
            return 0

        fired = 0
        for rule in self._rules_for(subject_type, subject_id, snapshot):
            fired += self._evaluate_rule(rule, subject_id, snapshot, ts)
        return fired

    def _snapshot(self, subject_type: str, subject_id: str) -> Optional[dict]:
        """The dict an extractor sees: last known state plus the logical clock."""
        if subject_type == "queue":
            row = self.conn.execute(
                "SELECT payload FROM queue_states WHERE queue_id = ?", (subject_id,)
            ).fetchone()
            if row is None:
                return None
            snapshot = json.loads(row["payload"])
        else:
            row = self._agent_row(subject_id)
            if row is None:
                return None
            snapshot = dict(row)
            snapshot["queue_ids"] = json.loads(row["queue_ids"])
        snapshot["now"] = _iso(self.now) if self.now else None
        return snapshot

    def _rules_for(self, subject_type: str, subject_id: str, snapshot: dict) -> list[sqlite3.Row]:
        """Rules whose scope covers this subject.

        At this size a scan is fine. At real volume the same filter is an index
        lookup on (subject_type, metric) plus a scope join; the shape of the
        predicate does not change, only where the matching happens.
        """
        # Ordered most severe first, which is what makes supersession
        # deterministic: when a breach and an at-risk rule both become true on
        # the same event, the breach is evaluated and sent first, so the
        # at-risk rule can see it and stand down.
        rows = self.conn.execute(
            "SELECT * FROM rules WHERE enabled = 1 AND subject_type = ?"
            " ORDER BY CASE severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2"
            "                        ELSE 1 END DESC, id",
            (subject_type,),
        ).fetchall()

        matched = []
        for rule in rows:
            scope_ids = json.loads(rule["scope_ids"])
            if rule["scope_type"] == "all":
                matched.append(rule)
            elif rule["scope_type"] == "ids" and subject_id in scope_ids:
                matched.append(rule)
            elif rule["scope_type"] == "queues":
                # "any of my agents": the agent serves one of these queues.
                if set(snapshot.get("queue_ids") or []) & set(scope_ids):
                    matched.append(rule)
        return matched

    def _evaluate_rule(self, rule: sqlite3.Row, subject_id: str, snapshot: dict, ts: datetime) -> int:
        metric = catalog.get(rule["metric"])
        if metric is None:
            return 0
        self._evaluated.add((rule["id"], subject_id))

        reading = metric.extract(snapshot)

        # Not applicable is a definite no, not ignorance. The agent hung up, so
        # the long-call condition is false and any open episode must close --
        # which is also what emits the "resolved" notice. Handled by falling
        # through to _sustain with holds=False.
        if reading is catalog.NOT_APPLICABLE:
            return self._sustain(rule, subject_id, None, False, ts)

        # Unknown is neither true nor false. We cannot say the condition holds
        # and we cannot say it broke, so we leave the episode exactly as it was
        # and record why. This is the null forecast, and the violation with no
        # start time.
        if reading is None:
            self._suppress(
                rule["id"], subject_id, "missing_data",
                f"{rule['metric']} unavailable", _iso(ts),
            )
            return 0

        holds = _compare(reading, rule["operator"], rule["threshold"])
        return self._sustain(rule, subject_id, reading, holds, ts)

    # ----------------------------------------------------------------- sustain

    def _sustain(self, rule: sqlite3.Row, subject_id: str, value: Optional[float],
                 holds: bool, ts: datetime) -> int:
        """Track how long the predicate has held, and fire when it has held long
        enough.

        An *episode* is one unbroken run of the predicate being true. It opens
        on the false -> true edge and closes on the true -> false edge. All
        notification decisions are made about the episode, never about the
        individual event, which is why a queue that breaches for an hour
        produces one alert rather than one per snapshot.
        """
        state = self.conn.execute(
            "SELECT * FROM condition_states WHERE rule_id = ? AND subject_id = ?",
            (rule["id"], subject_id),
        ).fetchone()

        was_open = bool(state["is_open"]) if state else False
        true_since = _parse(state["true_since"]) if state and state["true_since"] else None
        fired_at = _parse(state["fired_at"]) if state and state["fired_at"] else None

        # ---- predicate broke: close the episode.
        if not holds:
            # Still false, and it was already false. Nothing about the episode
            # has changed, so there is nothing to write. This is the common case
            # by a wide margin -- most rules are not firing for most subjects
            # most of the time -- and writing a row per evaluation would make
            # the quiet path the most expensive one in the system.
            if not was_open:
                return 0

            self._write_state(rule["id"], subject_id, False, None, None, 0, value, ts)
            # Only tell them it recovered if we told them it broke.
            if fired_at is not None and rule["notify_on_resolve"]:
                self._notify(rule, subject_id, value, ts, kind="resolved")
                return 1
            return 0

        # ---- predicate holds. Open the episode if this is the rising edge.
        if not was_open or true_since is None:
            true_since = ts

        held_for = (ts - true_since).total_seconds()

        # ---- not sustained long enough yet. Real, but not yet worth a ping.
        if held_for < rule["duration_sec"]:
            self._write_state(rule["id"], subject_id, True, true_since, fired_at,
                              state["fire_count"] if state else 0, value, ts)
            self._suppress(
                rule["id"], subject_id, "duration_not_met",
                f"held {catalog.humanize_seconds(held_for)} of "
                f"{catalog.humanize_seconds(rule['duration_sec'])}",
                _iso(ts),
            )
            return 0

        # ---- sustained. Ask the suppression layer whether we may speak.
        allowed, reason, detail, kind = self._may_notify(rule, subject_id, fired_at, ts)
        fire_count = state["fire_count"] if state else 0

        if not allowed:
            self._write_state(rule["id"], subject_id, True, true_since, fired_at, fire_count, value, ts)
            self._suppress(rule["id"], subject_id, reason, detail, _iso(ts))
            # A dropped alert must never be an invisible one. Rate limiting
            # collapses into a digest instead of deleting.
            if reason == "rate_limit":
                return self._collapse(rule, ts)
            return 0

        self._notify(rule, subject_id, value, ts, kind=kind, held_for=held_for)
        self._write_state(rule["id"], subject_id, True, true_since, ts, fire_count + 1, value, ts)
        return 1

    def _may_notify(self, rule: sqlite3.Row, subject_id: str,
                    fired_at: Optional[datetime], ts: datetime):
        """Four gates, cheapest first.

        1. already_open - we have already alerted for this episode. Silence
           until it resolves, unless the rule opted into reminders.
        2. cooldown    - reminders are allowed, but not before cooldown_sec.
        3. superseded  - a more severe rule about the same thing is already
           live and has already been sent.
        4. rate_limit  - a hard ceiling per recipient per hour, so no amount of
           badly written rules can bury someone.

        Every refusal carries a detail string as well as a reason. A count tells
        you the system was quiet; the detail tells you whether it was right to
        be, and it is the difference between an audit trail and a tally.
        """
        if fired_at is not None:
            since = catalog.humanize_seconds((ts - fired_at).total_seconds())
            if rule["cooldown_sec"] == 0:
                return (False, "already_open",
                        f"already alerted {since} ago; set to stay quiet until it resolves",
                        "alert")
            if (ts - fired_at).total_seconds() < rule["cooldown_sec"]:
                return (False, "cooldown",
                        f"last sent {since} ago, reminders every "
                        f"{catalog.humanize_seconds(rule['cooldown_sec'])}",
                        "alert")

        winner = self._superseding_rule(rule, subject_id)
        if winner is not None:
            return (False, "superseded",
                    f"'{winner['name']}' ({winner['severity']}) is already open "
                    f"on {subject_id} and covers the same metric",
                    "alert")

        severity = rule["severity"]
        cap = SEVERITY_HOURLY_CAP.get(severity, 12)
        recent = self._recent_count(rule["recipient_id"], severity, ts)
        if recent >= cap:
            return (False, "rate_limit",
                    f"{recent} {severity} notifications already sent this hour "
                    f"(budget {cap}); collapsed into a digest",
                    "alert")

        return True, "", None, ("reminder" if fired_at is not None else "alert")

    def _superseding_rule(self, rule: sqlite3.Row, subject_id: str) -> Optional[sqlite3.Row]:
        """The more severe rule already live on this subject, if there is one.

        Returns the winning rule rather than a boolean so the suppression row
        can name it. "Suppressed: superseded" invites the question; "superseded
        by 'SLA breached' (critical)" answers it.

        Overlapping thresholds on one metric are good practice -- warn at 80%
        of SLA, escalate at 100% -- but when a queue jumps straight past both,
        the lead gets two messages describing one situation, and the milder one
        adds nothing.

        The test is deliberately "currently open AND already sent" rather than
        a time window: the warning stays quiet for as long as the breach is
        genuinely live, and becomes eligible again the moment the breach clears
        while the queue is still at risk -- which is exactly when a lead wants
        to hear about it.

        Scoped to the same recipient, because suppressing one person's alert
        because a different person was told is never right.
        """
        return self.conn.execute(
            "SELECT r.name, r.severity FROM condition_states c"
            " JOIN rules r ON r.id = c.rule_id"
            " WHERE c.subject_id = ? AND r.metric = ? AND r.recipient_id = ?"
            "   AND r.id != ? AND r.enabled = 1"
            "   AND c.is_open = 1 AND c.fired_at IS NOT NULL"
            "   AND CASE r.severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END"
            "     > CASE ? WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END"
            " ORDER BY CASE r.severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2"
            "                          ELSE 1 END DESC"
            " LIMIT 1",
            (subject_id, rule["metric"], rule["recipient_id"], rule["id"], rule["severity"]),
        ).fetchone()

    def _recent_count(self, recipient_id: str, severity: str, ts: datetime) -> int:
        """Notifications already sent to this person at this severity in the
        last hour. Counted per severity so budgets stay independent."""
        since = _iso(ts - timedelta(hours=1))
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM notifications"
            " WHERE recipient_id = ? AND severity = ? AND kind != 'digest'"
            "   AND event_ts >= ?",
            (recipient_id, severity, since),
        ).fetchone()
        return row["n"]

    def _collapse(self, rule: sqlite3.Row, ts: datetime) -> int:
        """Fold rate-limited alerts into a single rolling digest.

        Dropping a notification silently is the one failure this system cannot
        afford, because the user has no way to know it happened. So when the
        budget is spent we still emit exactly one row per recipient per hour,
        and rewrite it in place as more alerts pile up behind it. The count and
        the highest severity held back are always current.
        """
        recipient_id = rule["recipient_id"]
        since = _iso(ts - timedelta(hours=1))

        stats = self.conn.execute(
            "SELECT COUNT(*) AS n, r.severity AS severity"
            " FROM suppressions s JOIN rules r ON r.id = s.rule_id"
            " WHERE s.reason = 'rate_limit' AND r.recipient_id = ? AND s.event_ts >= ?"
            " GROUP BY r.severity",
            (recipient_id, since),
        ).fetchall()

        held = sum(row["n"] for row in stats)
        top = max(
            (row["severity"] for row in stats),
            key=lambda s: SEVERITY_RANK.get(s, 0),
            default=rule["severity"],
        )
        title = "Notifications held back"
        body = (
            f"{held} alert{'s' if held != 1 else ''} suppressed in the last hour "
            f"to keep your inbox usable. Highest severity held back: {top}. "
            f"Open the rules page to review what is firing."
        )

        existing = self.conn.execute(
            "SELECT id FROM notifications"
            " WHERE recipient_id = ? AND kind = 'digest' AND event_ts >= ?"
            " ORDER BY id DESC LIMIT 1",
            (recipient_id, since),
        ).fetchone()

        if existing is not None:
            # Update in place rather than emitting a second digest: the digest
            # is a running tally, not an event.
            self.conn.execute(
                "UPDATE notifications SET body = ?, value = ?, event_ts = ? WHERE id = ?",
                (body, float(held), _iso(ts), existing["id"]),
            )
            return 0

        cursor = self.conn.execute(
            "INSERT INTO notifications"
            " (rule_id, rule_name, subject_id, recipient_id, channel, severity,"
            "  kind, title, body, value, event_ts, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'digest', ?, ?, ?, ?, ?)",
            (
                rule["id"], "Digest", "-", recipient_id, rule["channel"], top,
                title, body, float(held), _iso(ts), _iso(_utcnow()),
            ),
        )
        recipient = self.conn.execute(
            "SELECT * FROM users WHERE id = ?", (recipient_id,)
        ).fetchone()
        deliver(Notification(
            id=cursor.lastrowid,
            channel=rule["channel"],
            severity=top,
            kind="digest",
            title=title,
            body=body,
            recipient_name=recipient["name"] if recipient else recipient_id,
            recipient_handle=(recipient["slack_handle"] or recipient["email"]) if recipient else None,
            event_ts=_iso(ts),
        ))
        return 1

    def _write_state(self, rule_id, subject_id, is_open, true_since, fired_at,
                     fire_count, value, ts) -> None:
        self.conn.execute(
            "INSERT INTO condition_states"
            " (rule_id, subject_id, is_open, true_since, fired_at, fire_count, last_value, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (rule_id, subject_id) DO UPDATE SET"
            "  is_open = excluded.is_open, true_since = excluded.true_since,"
            "  fired_at = excluded.fired_at, fire_count = excluded.fire_count,"
            "  last_value = excluded.last_value, updated_at = excluded.updated_at",
            (
                rule_id, subject_id, 1 if is_open else 0,
                _iso(true_since) if true_since else None,
                _iso(fired_at) if fired_at else None,
                fire_count, value, _iso(ts),
            ),
        )

    # ------------------------------------------------------------------- sweep

    def _maybe_sweep(self) -> int:
        """Re-evaluate time-sensitive conditions on a timer.

        Two distinct things go stale between events, and both need this:

        1. Time-driven metrics, whose VALUE grows on its own. An agent goes
           on_call at 09:00 and sends nothing further; at 09:46 they are in
           breach of a 45-minute rule and no event will ever say so.

        2. Any open episode that has not fired yet, whose ELAPSED TIME grows on
           its own. Billing hits zero available agents at 09:15 with a 5-minute
           window. It qualifies at 09:20, but the next snapshot lands at 09:25.
           Without the sweep the alert is five minutes late, which for a
           coverage gap is most of the incident.

        Together these give one guarantee that holds for every rule regardless
        of subject: a duration rule fires within SWEEP_INTERVAL_SEC of its
        threshold, provided the feed is live. Cost is bounded by staffed agents
        plus open episodes, never by event volume.
        """
        if self.now is None:
            return 0
        if self._last_sweep is not None and (self.now - self._last_sweep).total_seconds() < SWEEP_INTERVAL_SEC:
            return 0
        self._last_sweep = self.now

        time_driven = catalog.time_driven_keys()
        work: dict[tuple[int, str], tuple[sqlite3.Row, str, dict]] = {}

        # (1) every agent, against rules whose metric moves with the clock.
        uses_time_driven = any(
            row["metric"] in time_driven
            for row in self.conn.execute(
                "SELECT DISTINCT metric FROM rules WHERE enabled = 1 AND subject_type = 'agent'"
            )
        )
        if uses_time_driven:
            for row in self.conn.execute("SELECT agent_id FROM agent_states"):
                snapshot = self._snapshot("agent", row["agent_id"])
                if snapshot is None:
                    continue
                for rule in self._rules_for("agent", row["agent_id"], snapshot):
                    key = (rule["id"], row["agent_id"])
                    if rule["metric"] in time_driven and key not in self._evaluated:
                        work[key] = (rule, row["agent_id"], snapshot)

        # (2) open episodes still waiting out their window, any subject type.
        pending = self.conn.execute(
            "SELECT c.subject_id, r.* FROM condition_states c"
            " JOIN rules r ON r.id = c.rule_id"
            " WHERE c.is_open = 1 AND c.fired_at IS NULL"
            "   AND r.enabled = 1 AND r.duration_sec > 0"
        ).fetchall()
        for rule in pending:
            key = (rule["id"], rule["subject_id"])
            if key in work or key in self._evaluated:
                continue
            snapshot = self._snapshot(rule["subject_type"], rule["subject_id"])
            if snapshot is not None:
                work[key] = (rule, rule["subject_id"], snapshot)

        # Deduplicated, so a subject caught by both passes is evaluated once.
        # Evaluating twice would fire, then immediately log a bogus cooldown
        # suppression against the alert we just sent.
        fired = 0
        for rule, subject_id, snapshot in work.values():
            fired += self._evaluate_rule(rule, subject_id, snapshot, self.now)
        return fired

    # ------------------------------------------------------------------ notify

    def _notify(self, rule: sqlite3.Row, subject_id: str, value: Optional[float],
                ts: datetime, kind: str, held_for: Optional[float] = None) -> None:
        metric = catalog.get(rule["metric"])
        recipient = self.conn.execute(
            "SELECT * FROM users WHERE id = ?", (rule["recipient_id"],)
        ).fetchone()

        shown = catalog.format_value(value, metric.unit)
        limit = catalog.format_value(rule["threshold"], metric.unit)
        # Render the person's name, not their id. The id stays the id in the
        # stored row and in every key -- this is a display concern only.
        subject = self._display_name(subject_id)
        body = metric.phrase.format(subject=subject, value=shown, threshold=limit)

        if kind == "resolved":
            title = f"Resolved: {rule['name']}"
            # A resolve can arrive with no reading at all: the agent hung up, so
            # "time on this call" no longer has a value. Say that plainly rather
            # than reporting "unknown", which reads as a bug to the person on
            # the other end.
            body = (
                f"{subject} is back within limits ({shown})."
                if value is not None
                else f"{subject} no longer meets this condition."
            )
        else:
            title = rule["name"] if kind == "alert" else f"Still open: {rule['name']}"
            if held_for and rule["duration_sec"] > 0:
                body += f", sustained {catalog.humanize_seconds(held_for)}"
            body += "."

        cursor = self.conn.execute(
            "INSERT INTO notifications"
            " (rule_id, rule_name, subject_id, recipient_id, channel, severity,"
            "  kind, title, body, value, event_ts, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rule["id"], rule["name"], subject_id, rule["recipient_id"],
                rule["channel"], rule["severity"], kind, title, body, value,
                _iso(ts), _iso(_utcnow()),
            ),
        )

        deliver(Notification(
            id=cursor.lastrowid,
            channel=rule["channel"],
            severity=rule["severity"],
            kind=kind,
            title=title,
            body=body,
            recipient_name=recipient["name"] if recipient else rule["recipient_id"],
            recipient_handle=(recipient["slack_handle"] or recipient["email"]) if recipient else None,
            event_ts=_iso(ts),
        ))

    def _display_name(self, subject_id: str) -> str:
        """The name a human would use for this subject.

        Agents get their roster name; queues are already readable, and anything
        we have never heard of falls back to its id rather than rendering blank.
        """
        row = self.conn.execute(
            "SELECT name FROM users WHERE agent_id = ?", (subject_id,)
        ).fetchone()
        return row["name"] if row else subject_id

    def _suppress(self, rule_id, subject_id, reason, detail, event_ts) -> None:
        self.conn.execute(
            "INSERT INTO suppressions (rule_id, subject_id, reason, detail, event_ts, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (rule_id, subject_id, reason, detail, event_ts, _iso(_utcnow())),
        )


# ------------------------------------------------------------------- helpers


def _compare(value: float, operator: str, threshold: float) -> bool:
    if operator == "gt":
        return value > threshold
    if operator == "gte":
        return value >= threshold
    if operator == "lt":
        return value < threshold
    if operator == "lte":
        return value <= threshold
    return value == threshold


def _subject_id(event: AnyEvent) -> str:
    return event.queue_id if isinstance(event, QueueSnapshot) else event.agent_id


def _subject_type(event: AnyEvent) -> str:
    return "queue" if isinstance(event, QueueSnapshot) else "agent"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

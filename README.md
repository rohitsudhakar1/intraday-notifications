# Intraday Notification System

Rule configuration, evaluation against a live event stream, and routing to the right person
without becoming noise.

```
94 events accepted · 1 duplicate rejected · 1 stale rejected
25 notifications sent · 83 suppressions recorded, every one with a written reason
39 tests
```

---

## Run it

```bash
make install      # venv + pip install + npm install
make build        # build the React app so the API can serve it
make api          # http://127.0.0.1:8000/app   (API docs at /docs)
```

Then in a second terminal:

```bash
make replay       # streams the morning at 120x — watch it happen
make replay-fast  # same, no waiting
make test         # 39 tests
make reset        # wipe the database and start over
```

`make api` seeds four users and nine rules on first run. For frontend work, `make web` runs
the Vite dev server on :5173 and proxies `/api` to the backend.

## What to look at

1. **Open `/app` as Priya (billing lead)** and run the replay. The incident arc builds:
   coverage gap → SLA at risk → breach → backlog, then resolves as it recovers.
2. **Switch to Jordan (agent a_19)** in the top-right picker. Three messages for a 35-minute
   adherence drift: a nudge, a reminder, a resolve. Same event stream, completely different inbox.
3. **Watch "Why it stayed quiet."** 25 sent, 83 suppressed, each with a reason in English.
4. **Create a rule.** It reads as a sentence and previews what you'll receive before you save.
5. **Try to break it.** Set an SLA threshold of `80`. The error tells you what you meant.

---

## Product decisions

### Who this is for: team leads and agents

**Leads** are the people who can actually act intraday — pull someone off a break, move an
agent between queues, escalate. They watch queues and their own team.

**Agents** care about one thing: themselves. Most adherence breaks are accidental, so telling
the agent *first* means most problems get fixed without a manager involved. The seeded rules
give the agent a 10-minute fuse and their lead a 20-minute one, so `a_88` is nudged at 10:15
and Marcus only hears about it at 10:25. That routing is configuration, not code.

**Heads of support are deliberately out of scope.** They want a scheduled digest — a different
surface with aggregation and trend comparison. It would have been a second half-finished
feature, so I cut it rather than stub it.

### Rule configuration: flat predicate rows

Every rule in this domain is the same sentence:

> for **&lt;subject&gt;** in **&lt;scope&gt;**, when **&lt;metric&gt; &lt;operator&gt; &lt;threshold&gt;** holds for
> **&lt;duration&gt;**, tell **&lt;recipient&gt;** on **&lt;channel&gt;**

"Billing has more than 20 tickets waiting" and "an agent has been on one call over 45 minutes"
are the same shape with different values. So: one table, one evaluator, no per-rule-type code.

I considered two alternatives and rejected both:

- **Typed templates per rule kind** — clean UI, but "how do I add a rule type?" is answered
  with "I write new code," and every template needs its own evaluator.
- **A nested boolean condition tree** — maximum flexibility, but the UI becomes a query builder,
  users can author rules that never fire, and it's a hand-rolled query engine to maintain.

The normalised predicate costs about twenty extra lines over templates and gets template-quality
UX anyway, because the UI renders itself from a metric catalog. Adding a rule type is a catalog
entry. Nesting would be speculative — no rule in the brief needs it, and if power users ever
demand composition, a predicate becomes a leaf and nothing is thrown away.

---

## How it works

```
ingest → apply → evaluate → sustain → suppress → deliver
```

### The episode

An **episode** is one unbroken run of a rule's condition being true. It opens on the
false→true edge and closes on the true→false edge, and is stored as one row per
`(rule_id, subject_id)`:

| column | answers |
| --- | --- |
| `true_since` | how long has this been going on? |
| `fired_at` | have we already said something? |

**Every notification decision is made about the episode, never about the event.** Billing
breaches at 09:30 and stays broken until 10:15. Snapshots arrive every 30 seconds, so the
condition is true about 90 separate times — one episode, one alert.

Both columns are cleared together on the falling edge, which is what makes a recovery-then-relapse
a genuinely new incident rather than a continuation.

### The logical clock

The engine's `now` is the newest event timestamp seen, not wall-clock time. Replaying a file
reproduces exactly what happened, and tests advance an hour in a microsecond and assert exact
timestamps with no `sleep()`.

### The timer sweep

Some conditions become true while nothing is happening. An agent goes `on_call` at 09:00 and
the feed says nothing more; at 09:46 they're past a 45-minute limit and **no event will ever
say so.** Separately, an open episode's elapsed time grows on its own — billing loses coverage
at 09:15 with a 5-minute window, qualifies at 09:20, but the next snapshot is at 09:25.

Both are handled by re-checking every 60 seconds of logical time, which gives one guarantee
that holds for every rule regardless of subject:

> **Duration rules fire within 60 seconds of their threshold**, provided the feed is live.

The cost is bounded by staffed agents plus open episodes, never by event volume.

This turned out to be a correctness fix rather than a latency optimisation — see below.

---

## Noise control

Four layers, each with its own reason code. Every refusal writes a row to `suppressions` with
a sentence a human can read.

| Layer | Stops | In the demo run |
| --- | --- | --- |
| **Episode** (edge-triggered) | 120 chances → 1 alert | `already_open` |
| **Cooldown** (per rule) | silence on long incidents — repeats arrive as `reminder`, titled "Still open" | 44 |
| **Supersede by severity** | a breach and its own at-risk warning both firing | 14 |
| **Per-severity hourly budgets** | one bad config burying someone | tested, not reached |

Plus three "not yet / can't tell" gates: `duration_not_met` (16), `missing_data` (7), and the
two ingest gates `duplicate_event` and `stale_event` (1 each).

### Rate limits are per severity, not flat

```python
SEVERITY_HOURLY_CAP = {"info": 6, "warning": 12, "critical": 40}
```

A flat cap has a specific failure mode: twelve routine adherence nudges arrive first, and the
thirteenth notification — the VIP queue going critical — is silently dropped. That's the noise
problem inverted, and worse, because the system looks like it's working. Independent budgets
mean routine traffic can never crowd out a critical.

Critical has a ceiling of 40 rather than an unlimited bypass. Episode de-duplication plus a
30-minute cooldown already bound one rule to ~2/hour, so reaching 40 needs dozens of *distinct*
critical conditions — a genuine outage where every message is worth sending. But an unbounded
channel isn't something to ship on purpose.

### Suppressed never means invisible

When the budget is spent we don't delete the alert — we emit a digest ("8 alerts suppressed in
the last hour, highest severity warning") and **rewrite it in place** as more pile up. One row
per recipient per hour. If it were emitted per suppression, the anti-noise mechanism would
itself become a noise source.

Only `rate_limit` gets a digest. The other reasons lose nothing: `already_open` and `cooldown`
mean you were already told, `superseded` means you got the more severe version, `duration_not_met`
means nothing has happened yet.

### Supersession

Overlapping thresholds on one metric are good practice — warn at 80% of SLA, escalate at 100%.
But when a queue jumps past both at once, the lead gets two messages describing one situation.

If a more severe rule on the same `(metric, subject, recipient)` is currently open *and has
already been sent*, the milder one stands down. The test is deliberately "currently open" rather
than a time window, so the warning becomes eligible again the moment the breach clears while
the queue is still at risk. The real data shows both halves: the 09:35 double disappears, and
"SLA at risk: billing at 92%" correctly reappears at 10:18 once the breach resolves.

---

## The sample feed has five defects

They're the interesting part of the data, so `replay.py` sends the file in its original order
by default rather than sorting.

| # | Defect | Handling |
| --- | --- | --- |
| 1 | `evt_01HXYZ050` appears **twice** | `event_id` is the PRIMARY KEY — the duplicate collides on insert. Matters beyond double-sending: applying it twice could reopen a closed episode and corrupt the duration state every other rule depends on. |
| 2 | Last two events are **out of order** (09:36, 09:49 arriving after 10:30) | Compared against `last_event_ts` per subject. Stored but marked `stale`, never allowed to drive state — applying an old event over newer state rewrites history. |
| 3 | `queue_ids: null` in one event, `[]` in another | Normalised to `[]` by a Pydantic validator. The engine keeps the last known queue list rather than overwriting with empty, which would silently drop that agent out of every scoped rule. |
| 4 | `volume_forecast_next_15m: null` | The extractor returns `None`, the rule is skipped, the reason is logged. Treating null as `0` makes the ratio infinite. |
| 5 | `in_violation: true` with `violation_started_at: null` | The duration is real but unmeasurable, so we skip and say so rather than guessing it just started. Fires 6 times in the run. |

**The principle that came out of 4 and 5:** *unknown is not false.* If we don't know the value we
can neither say the condition holds nor that it broke, so the episode is left exactly as it was.

There's a third state too. An extractor returns `NOT_APPLICABLE` when the metric genuinely doesn't
apply — the agent isn't on a call, so call length is meaningless. That's a definite negative, and
it *closes* the episode. Collapsing it into `None` leaves a long-call episode open forever after
the agent hangs up, silently blocking every future alert for that pair.

---

## Data model

Seven tables. `rules` is the hub; `condition_states` is the system.

```
users ◄──── rules ◄──── condition_states   ★ the episode
                 ◄──── notifications        what we sent
                 ◄──── suppressions         what we didn't, and why

events ──► queue_states     joined by subject_id, deliberately not a foreign key:
       ──► agent_states     a queue exists in the feed whether or not we've seen it
```

`agent_states` exists because **no single event can answer "has this agent been on one call for
45 minutes?"** Nothing in the feed says that — we remember `entered_at` and measure against the
clock. `entered_at` only moves when the state actually changes, so a chatty upstream re-sending
"still available" can't starve a long-call rule forever.

`queue_states` keeps the whole snapshot payload, so derived metrics (SLA ratio, volume against
forecast) recompute without re-reading the event log.

`condition_states` is only written when something about the episode actually changes. Most rules
are not firing for most subjects most of the time, and writing a row per evaluation would make the
quiet path the most expensive one in the system — on this feed the guard removes 84% of writes
(663 → 105) with identical output.

**Names.** The feed identifies people as `a_11`; nobody on a support floor talks that way, so
`users` doubles as a roster and notifications render "Alex Chen has been on one call for 50m". In a
real deployment that table syncs from the customer's workforce system. The id stays the id
everywhere it matters — episode keys, joins, de-duplication — and the name is used only in the
rendered message.

### The metric catalog

A rule row stores `metric` as a string; `app/catalog.py` is the lookup table that says what it
means — where the value comes from, what to call it, how to phrase it. Adding a rule type is a
catalog entry, and `/api/catalog` feeds the rule builder so a new metric appears in the UI with
no frontend change.

`sla_ratio` is worth calling out: billing's SLA is 120s, tier 2's is 300s, VIP's is 60s. Comparing
`longest_wait_sec ÷ sla_target_sec` means **one** rule (`> 1.0`) covers every queue and keeps
working when an SLA changes, instead of three rules with three hardcoded numbers that quietly go
stale.

---

## Testing

39 tests, all on an in-memory database with no sleeping — the logical clock lets a test advance
an hour instantly and assert exact timestamps.

| File | | Pins down |
| --- | --- | --- |
| `test_suppression.py` | 11 | Silence is a decision. A queue breaching for an hour = 1 alert, not 120. Criticals survive a wall of warnings. Nothing suppressed is invisible. |
| `test_duration.py` | 8 | "Continuously for 10 minutes" means one unbroken run — 9 min + a dip + 9 min is **not** 18. A long call fires with no event to announce it. |
| `test_ingest.py` | 7 | Every defect above, each docstring citing the event it comes from. |
| `test_rules.py` | 13 | A rule that can never fire is worse than no rule. |

Deliberately **not** tested: the CRUD endpoints (FastAPI and Pydantic own that), SQLite itself,
and the notifier stubs — except that a broken channel must not stall ingestion, which is tested.
Testing those would have inflated the count and proved nothing.

---

## Scaling

| Today | At real volume |
| --- | --- |
| `POST /api/events` fed by a script | A Kafka/Kinesis consumer sits exactly where the script sits. **The engine doesn't change** — that's the point of having a real ingestion boundary. |
| SQLite | Postgres. The DDL is already written Postgres-shaped; JSON columns become `JSONB`. |
| One engine, one lock | Partition by `subject_id`. All state is keyed by `subject_id` or `(rule_id, subject_id)`, so a queue or agent is owned by exactly one worker with no cross-talk. |
| `_rules_for` scans all rules | Index lookup on `(subject_type, metric)` plus a scope join. The predicate shape doesn't change, only where matching happens — `idx_rules_lookup` already exists. |
| Sweep walks all agents | Already bounded by **staff count, not event volume**. Becomes a per-shard timer. |
| UI polls every 2s | Websocket or SSE per recipient. |

The property that makes this work: every piece of engine state is keyed by subject. No global
accumulator, no shared counter, nothing requiring two workers to agree. The one exception is the
per-recipient rate limit, keyed by recipient — at scale that's a small Redis counter.

---

## Known limitations

**One predicate per rule — no `AND`.** "No one available on billing" can't add "*and* tickets are
waiting." The mitigation today is the sustain window: requiring five minutes filters out the
harmless case. The honest next step is a *second* predicate on the same row, not a recursive tree —
two conditions covers every realistic support rule, and arbitrary nesting would be speculative.

**The clock advances with events.** The 60s guarantee holds provided the feed is live. At the
stated cadences (snapshots every 30s, adherence checks every 60s) it never binds; in production
the sweep would also be driven by a wall-clock timer.

**Rule editing is restricted.** You can change threshold, duration, cooldown, severity, channel and
enabled — but not what a rule *measures*. Changing the metric in place would leave open episodes
tracking a predicate that no longer exists and make the notification history a lie. Changing the
threshold or window clears open episodes for the same reason.

### What I'd build next

1. **A "why did this fire?" drill-down.** Click a notification, see the rule, the reading, the
   episode timeline and every suppression along the way. The data already exists — it's a read-only view.
2. **A second predicate per rule.**
3. **Starter templates on first run** — a new team shouldn't face an empty rules page.
4. **Snooze** — "quiet for 30 minutes, I'm on it." One column, one more gate.
5. **The digest surface for heads of support**, the audience I cut.

---

## AI use

I used **Claude Code** throughout, as a pair programmer working to my direction rather than as a
code generator I accepted output from.

**The decisions were mine, and they're the parts of this system I'd defend hardest:**

- **Flat predicate rows over typed templates or a condition tree.** I made the call that every rule
  in this domain reduces to the same shape, so a normalised row costs ~20 lines over templates and
  turns "how do I add a rule type?" into "a catalog entry" — while explicitly skipping nested
  boolean trees as speculative.
- **Per-severity rate limits instead of a flat cap**, after identifying the failure mode where
  twelve routine nudges silently drop the one critical that mattered — and requiring that the
  overflow *collapse into a digest* rather than disappear.
- **Supersede-by-severity**, after reading the demo output and spotting that the lead received two
  messages describing one situation.
- **Stating the sweep interval as a guarantee** ("duration rules fire within 60s of their
  threshold") rather than documenting it as a limitation.
- **Scoping to leads and agents** and cutting the head-of-support digest.
- Python/FastAPI/React, and a real ingest endpoint plus a replay script rather than a file reader.

**Where I directed implementation:** I reviewed the code as it was written and sent it back when it
was wrong. Two examples worth naming — I caught that 58 of 83 suppression rows stored a reason but
a `NULL` detail, which made two-thirds of my best demo artifact a tally rather than an explanation;
and that the test suite was writing to the same `notifications.log` as the demo, so a reviewer
running tests first would open a confusing file.

**How I verified it:** by running it and reading the output against the raw feed, not by trusting
the tests. Six of the eight bugs found came from that, including two the tests would never have
caught:

- **A `missing_data` count of 385** on a 96-event file was implausible. Breaking it down showed only
  *one* was genuine — the rest were "not applicable" being conflated with "unknown." Underneath was
  a real bug: a long-call episode stayed open forever after the agent hung up, silently blocking
  every future alert for them. That's where the three-valued extractor came from.
- **A tier 2 SLA breach that generated zero alerts.** The queue breached at 10:00 and recovered
  before its next snapshot at 10:15, and queue rules only re-evaluated on snapshots. A 15-minute
  breach, invisible. That's what turned the timer sweep from a latency optimisation into a
  correctness fix.

I also wrote a test that failed and turned out to be wrong rather than the code — 70 minutes of
breach with a 30-minute cooldown correctly produces three messages, not two — and strengthened it
to assert the exact timestamps instead of just the count.

**Where I didn't use AI:** the product scoping and the trade-off calls above, and the judgment about
what to leave out.

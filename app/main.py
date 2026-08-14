"""HTTP API.

Three surfaces:

  POST /api/events         ingest. In production this is fed by a stream
                           consumer; here it is fed by scripts/replay.py.
  /api/rules               the rule configuration CRUD the product is built on.
  /api/notifications       what was sent, and /api/suppressions for what was
                           deliberately not sent.

The engine is created once and shared. SQLite serialises writes anyway, so a
single lock around ingestion is honest rather than limiting: at real volume the
engine is partitioned by subject_id and this lock becomes one per shard.
"""

import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import catalog, db
from .engine import Engine
from .models import (
    AnyEvent, EventEnvelope, IngestResult, NotificationRead,
    RuleCreate, RuleRead, RuleUpdate,
)

conn = db.connect()
db.init_db(conn)
engine = Engine(conn)
_ingest_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Rebuild the logical clock from the event log so a restart does not make
    # every open episode look brand new.
    row = conn.execute("SELECT MAX(ts) AS ts FROM events").fetchone()
    if row and row["ts"]:
        from .engine import _parse
        engine.now = _parse(row["ts"])
    yield


app = FastAPI(title="Intraday Notifications", version="1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ------------------------------------------------------------------- ingest


@app.post("/api/events", response_model=IngestResult)
def ingest(payload: EventEnvelope | AnyEvent = Body(...)):
    """Accepts one event or a batch. Idempotent on event_id."""
    events = payload.events if isinstance(payload, EventEnvelope) else [payload]
    with _ingest_lock:
        totals = engine.ingest_many(events)
    return IngestResult(**totals)


# -------------------------------------------------------------------- rules


def _row_to_rule(row) -> RuleRead:
    return RuleRead(
        id=row["id"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        subject_type=row["subject_type"],
        metric=row["metric"],
        operator=row["operator"],
        threshold=row["threshold"],
        duration_sec=row["duration_sec"],
        scope_type=row["scope_type"],
        scope_ids=json.loads(row["scope_ids"]),
        recipient_id=row["recipient_id"],
        recipient_name=row["recipient_name"] if "recipient_name" in row.keys() else None,
        channel=row["channel"],
        severity=row["severity"],
        cooldown_sec=row["cooldown_sec"],
        notify_on_resolve=bool(row["notify_on_resolve"]),
        created_at=row["created_at"],
        summary=catalog.describe(
            row["metric"], row["operator"], row["threshold"], row["duration_sec"]
        ),
    )


RULE_SELECT = (
    "SELECT r.*, u.name AS recipient_name FROM rules r"
    " LEFT JOIN users u ON u.id = r.recipient_id"
)


@app.get("/api/rules", response_model=list[RuleRead])
def list_rules(recipient_id: Optional[str] = None, enabled_only: bool = False):
    sql, params = RULE_SELECT, []
    clauses = []
    if recipient_id:
        clauses.append("r.recipient_id = ?")
        params.append(recipient_id)
    if enabled_only:
        clauses.append("r.enabled = 1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY r.id DESC"
    return [_row_to_rule(row) for row in conn.execute(sql, params).fetchall()]


@app.post("/api/rules", response_model=RuleRead, status_code=201)
def create_rule(rule: RuleCreate):
    user = conn.execute("SELECT id FROM users WHERE id = ?", (rule.recipient_id,)).fetchone()
    if user is None:
        raise HTTPException(400, f"unknown recipient '{rule.recipient_id}'")

    from .engine import _iso, _utcnow
    cursor = conn.execute(
        "INSERT INTO rules (name, enabled, subject_type, metric, operator, threshold,"
        " duration_sec, scope_type, scope_ids, recipient_id, channel, severity,"
        " cooldown_sec, notify_on_resolve, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rule.name, int(rule.enabled), rule.subject_type, rule.metric, rule.operator,
            rule.threshold, rule.duration_sec, rule.scope_type, json.dumps(rule.scope_ids),
            rule.recipient_id, rule.channel, rule.severity, rule.cooldown_sec,
            int(rule.notify_on_resolve), _iso(_utcnow()),
        ),
    )
    conn.commit()
    return _get_rule_or_404(cursor.lastrowid)


@app.patch("/api/rules/{rule_id}", response_model=RuleRead)
def update_rule(rule_id: int, patch: RuleUpdate):
    fields = patch.model_dump(exclude_unset=True)
    if not fields:
        return _get_rule_or_404(rule_id)

    existing = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if existing is None:
        raise HTTPException(404, "rule not found")

    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
    conn.execute(f"UPDATE rules SET {assignments} WHERE id = ?", [*values, rule_id])

    # Changing the threshold or the window redefines the predicate, so any open
    # episode was measured against a rule that no longer exists. Clearing it
    # means the next evaluation starts a clean episode instead of firing on
    # stale arithmetic.
    if {"threshold", "duration_sec"} & fields.keys():
        conn.execute("DELETE FROM condition_states WHERE rule_id = ?", (rule_id,))
    conn.commit()
    return _get_rule_or_404(rule_id)


@app.delete("/api/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int):
    conn.execute("DELETE FROM condition_states WHERE rule_id = ?", (rule_id,))
    conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    conn.commit()


def _get_rule_or_404(rule_id: int) -> RuleRead:
    row = conn.execute(RULE_SELECT + " WHERE r.id = ?", (rule_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "rule not found")
    return _row_to_rule(row)


# ------------------------------------------------------------------ catalog


@app.get("/api/catalog")
def get_catalog():
    """Everything the rule builder needs to render itself. The UI has no
    hardcoded metric list, so a new catalog entry shows up in the form."""
    return {
        "metrics": [
            {
                "key": metric.key,
                "label": metric.label,
                "subject_type": metric.subject_type,
                "unit": metric.unit,
                "help_text": metric.help_text,
                "time_driven": metric.time_driven,
            }
            for metric in catalog.METRICS.values()
        ],
        "operators": [{"key": k, "label": v} for k, v in catalog.OPERATOR_WORDS.items()],
        "queues": [row["queue_id"] for row in
                   conn.execute("SELECT queue_id FROM queue_states ORDER BY queue_id")],
        "agents": [row["agent_id"] for row in
                   conn.execute("SELECT agent_id FROM agent_states ORDER BY agent_id")],
    }


@app.get("/api/users")
def list_users():
    return [dict(row) for row in conn.execute("SELECT * FROM users ORDER BY role, name")]


# ------------------------------------------------------ notifications & audit


@app.get("/api/notifications", response_model=list[NotificationRead])
def list_notifications(
    recipient_id: Optional[str] = None,
    limit: int = Query(default=100, le=500),
):
    sql = "SELECT * FROM notifications"
    params = []
    if recipient_id:
        sql += " WHERE recipient_id = ?"
        params.append(recipient_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [NotificationRead(**dict(row)) for row in conn.execute(sql, params).fetchall()]


@app.get("/api/suppressions")
def list_suppressions(limit: int = Query(default=200, le=1000)):
    """Why we stayed quiet. The counts are the headline: they are the evidence
    that silence was a decision rather than a failure."""
    rows = conn.execute(
        "SELECT reason, COUNT(*) AS count FROM suppressions GROUP BY reason ORDER BY count DESC"
    ).fetchall()
    recent = conn.execute(
        "SELECT s.*, r.name AS rule_name FROM suppressions s"
        " LEFT JOIN rules r ON r.id = s.rule_id ORDER BY s.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {
        "by_reason": [dict(row) for row in rows],
        "recent": [dict(row) for row in recent],
    }


@app.get("/api/state")
def get_state():
    """Live world state, for the dashboard header."""
    return {
        "clock": engine.now.isoformat().replace("+00:00", "Z") if engine.now else None,
        "queues": [
            {**json.loads(row["payload"]), "last_event_ts": row["last_event_ts"]}
            for row in conn.execute("SELECT * FROM queue_states ORDER BY queue_id")
        ],
        "agents": [
            {
                "agent_id": row["agent_id"],
                "state": row["state"],
                "entered_at": row["entered_at"],
                "queue_ids": json.loads(row["queue_ids"]),
                "in_violation": bool(row["in_violation"]),
                "violation_started_at": row["violation_started_at"],
            }
            for row in conn.execute("SELECT * FROM agent_states ORDER BY agent_id")
        ],
        "open_episodes": [
            dict(row) for row in conn.execute(
                "SELECT c.*, r.name AS rule_name FROM condition_states c"
                " JOIN rules r ON r.id = c.rule_id WHERE c.is_open = 1"
            )
        ],
    }


@app.post("/api/reset", status_code=204)
def reset():
    """Wipe everything except rules and users, so a demo can be re-run."""
    for table in ("events", "notifications", "suppressions", "condition_states",
                  "agent_states", "queue_states"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    engine.now = None
    engine._last_sweep = None


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/app")


# The built React app is served from the API so the demo is one process. In
# development `npm run dev` proxies /api here instead, and this mount is simply
# unused. Registered last so it can never shadow an /api route.
_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if _DIST.is_dir():
    app.mount("/app", StaticFiles(directory=_DIST, html=True), name="web")
else:
    @app.get("/app", include_in_schema=False)
    def missing_build():
        return HTMLResponse(
            "<body style='font:15px system-ui;padding:40px;max-width:38em'>"
            "<h2>The web app has not been built yet.</h2>"
            "<p>Run <code>npm install &amp;&amp; npm run build</code> in <code>web/</code>, "
            "or <code>npm run dev</code> for the dev server on port 5173.</p>"
            "<p>The API is running: see <a href='/docs'>/docs</a>.</p></body>",
            status_code=503,
        )

import { useState } from "react";
import type { Notification, NotificationKind, OpenEpisode, Severity, User } from "../api";
import { clockTime } from "../api";

const KIND_LABEL: Record<NotificationKind, string> = {
  alert: "new",
  reminder: "still open",
  resolved: "resolved",
  digest: "held back",
};

const SEVERITY_RANK: Record<string, number> = { critical: 3, warning: 2, info: 1 };

function tone(notification: Notification): string {
  if (notification.kind === "resolved") return "ok";
  return notification.severity;
}

/** What a lead wants mid-incident is not "criticals" — a resolved critical is
 *  the good news, and it sits at the top of a severity-sorted list getting in
 *  the way. What they want is what is still broken, worst first.
 *
 *  A notification is still live if the episode that produced it is open. The
 *  engine already tracks that, so we match on (rule_id, subject_id) rather than
 *  guessing from the message. */
function stillBroken(notifications: Notification[], openEpisodes: OpenEpisode[]) {
  const live = new Set(openEpisodes.map((e) => `${e.rule_id}:${e.subject_id}`));
  return notifications
    .filter((n) => n.kind === "alert" || n.kind === "reminder")
    .filter((n) => live.has(`${n.rule_id}:${n.subject_id}`))
    .sort(
      (a, b) =>
        (SEVERITY_RANK[b.severity] ?? 0) - (SEVERITY_RANK[a.severity] ?? 0) ||
        b.event_ts.localeCompare(a.event_ts),
    );
}

/** An empty inbox has three different causes and only one of them is a problem
 *  the reader can act on. Telling someone to run the replay when they have
 *  simply never made a rule sends them off to fix the wrong thing. */
function emptyState(ruleCount: number, feedHasRun: boolean) {
  if (!feedHasRun) {
    return (
      <p className="empty">
        No events yet. Run <code>python scripts/replay.py</code> to play the morning through.
      </p>
    );
  }
  if (ruleCount === 0) {
    return (
      <p className="empty">
        Nothing is set up to notify you. Open <b>Rules</b> to create one.
      </p>
    );
  }
  return (
    <p className="empty">
      All quiet — none of your {ruleCount} rule{ruleCount === 1 ? "" : "s"} has fired.
    </p>
  );
}

export function Inbox({
  notifications,
  user,
  ruleCount,
  feedHasRun,
  openEpisodes,
}: {
  notifications: Notification[];
  user: User | undefined;
  ruleCount: number;
  feedHasRun: boolean;
  openEpisodes: OpenEpisode[];
}) {
  const [onlyLive, setOnlyLive] = useState(false);

  const live = stillBroken(notifications, openEpisodes);
  const shown = onlyLive ? live : notifications;

  return (
    <section className="card">
      <header>
        <h2>Inbox</h2>
        <p>
          {user ? `${user.name} · ${user.slack_handle ?? user.email ?? ""}` : ""}
          {notifications.length > 0 &&
            ` · ${notifications.length} message${notifications.length === 1 ? "" : "s"}`}
        </p>

        <div className="spacer" />

        <div className="tabs">
          <button data-active={!onlyLive} onClick={() => setOnlyLive(false)}>
            Everything
          </button>
          <button data-active={onlyLive} onClick={() => setOnlyLive(true)}>
            Needs attention {live.length > 0 && `(${live.length})`}
          </button>
        </div>
      </header>

      {notifications.length === 0 ? (
        emptyState(ruleCount, feedHasRun)
      ) : shown.length === 0 ? (
        <p className="empty">
          Nothing is broken right now. Everything here has resolved.
        </p>
      ) : (
        <div className="feed">
          {shown.map((notification) => (
            <article
              key={notification.id}
              className="note"
              data-severity={notification.severity as Severity}
              data-kind={notification.kind}
            >
              <time dateTime={notification.event_ts}>{clockTime(notification.event_ts)}</time>

              <div>
                <h3>{notification.title}</h3>
                <p>{notification.body}</p>
              </div>

              <div className="meta">
                <span className="pill" data-tone={tone(notification)}>
                  {KIND_LABEL[notification.kind]}
                </span>
                <span className="pill" data-tone="muted">
                  {notification.channel}
                </span>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

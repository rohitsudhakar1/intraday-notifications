import type { Notification, NotificationKind, Severity, User } from "../api";
import { clockTime } from "../api";

const KIND_LABEL: Record<NotificationKind, string> = {
  alert: "new",
  reminder: "still open",
  resolved: "resolved",
  digest: "held back",
};

function tone(notification: Notification): string {
  if (notification.kind === "resolved") return "ok";
  return notification.severity;
}

export function Inbox({
  notifications,
  user,
}: {
  notifications: Notification[];
  user: User | undefined;
}) {
  const open = notifications.filter((n) => n.kind === "alert" || n.kind === "reminder");

  return (
    <section className="card">
      <header>
        <h2>Inbox</h2>
        <p>
          {user ? `${user.name} · ${user.slack_handle ?? user.email ?? ""}` : ""}
          {notifications.length > 0 &&
            ` · ${notifications.length} message${notifications.length === 1 ? "" : "s"}, ${open.length} still open`}
        </p>
      </header>

      {notifications.length === 0 ? (
        <p className="empty">
          Nothing yet. Start the API and run <code>python scripts/replay.py</code> to
          play the morning through.
        </p>
      ) : (
        <div className="feed">
          {notifications.map((notification) => (
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

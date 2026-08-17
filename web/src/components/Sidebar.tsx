import { clockTime, humanizeSeconds } from "../api";
import type { AgentState, QueueState, Suppressions, WorldState } from "../api";

// The suppression panel is the unusual one, and it is deliberate. Any alerting
// system can show what it sent. Showing what it deliberately withheld, and why,
// is what lets someone trust that the quiet is real.
const REASON_TEXT: Record<string, string> = {
  already_open: "already told you — still broken",
  cooldown: "waiting out the repeat interval",
  duration_not_met: "true, but not for long enough yet",
  missing_data: "the feed did not report a value",
  superseded: "a more severe alert already covers it",
  rate_limit: "over the hourly budget, collapsed into a digest",
  duplicate_event: "the same event arrived twice",
  stale_event: "arrived after newer data",
};

function slaFraction(queue: QueueState): number {
  if (!queue.longest_wait_sec || !queue.sla_target_sec) return 0;
  return queue.longest_wait_sec / queue.sla_target_sec;
}

function slaColor(fraction: number): string {
  if (fraction >= 1) return "var(--critical)";
  if (fraction >= 0.8) return "var(--warning)";
  return "var(--ok)";
}

function secondsBetween(from: string, to: string | null): number {
  if (!to) return 0;
  return (Date.parse(to) - Date.parse(from)) / 1000;
}

export function Sidebar({
  state,
  suppressions,
}: {
  state: WorldState | null;
  suppressions: Suppressions | null;
}) {
  const agents = state?.agents ?? [];
  const inViolation = agents.filter((agent) => agent.in_violation);

  return (
    <aside>
      <section className="card">
        <header>
          <h2>Queues</h2>
          <p>as of {clockTime(state?.clock ?? null)}</p>
        </header>
        <div className="card-body">
          {(state?.queues ?? []).length === 0 ? (
            <p className="hint">No snapshots yet.</p>
          ) : (
            state!.queues.map((queue) => {
              const fraction = slaFraction(queue);
              return (
                <div className="queue-row" key={queue.queue_id}>
                  <div className="queue-head">
                    <b>{queue.queue_id}</b>
                    <span
                      className="pill"
                      data-tone={
                        fraction >= 1 ? "critical" : fraction >= 0.8 ? "warning" : "ok"
                      }
                    >
                      {Math.round(fraction * 100)}% of SLA
                    </span>
                  </div>
                  <div className="queue-facts">
                    <span>{queue.tickets_waiting ?? "—"} waiting</span>
                    <span>{humanizeSeconds(queue.longest_wait_sec ?? 0)} oldest</span>
                    <span>{queue.agents_available ?? "—"} free</span>
                  </div>
                  <div className="bar">
                    <div
                      style={{
                        width: `${Math.min(100, fraction * 100)}%`,
                        background: slaColor(fraction),
                      }}
                    />
                  </div>
                </div>
              );
            })
          )}
        </div>
      </section>

      <section className="card">
        <header>
          <h2>Agents</h2>
          <p>
            {agents.length} tracked
            {inViolation.length > 0 && ` · ${inViolation.length} out of adherence`}
          </p>
        </header>
        <div className="card-body">
          {agents.length === 0 ? (
            <p className="hint">No agent activity yet.</p>
          ) : (
            agents.map((agent: AgentState) => (
              <div className="agent-row" key={agent.agent_id}>
                <span>
                  {agent.name}{" "}
                  <span style={{ color: "var(--ink-soft)" }}>{agent.state.replace("_", " ")}</span>
                </span>
                <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  {agent.in_violation && (
                    <span className="pill" data-tone="warning">
                      adherence
                    </span>
                  )}
                  <span className="dur">
                    {humanizeSeconds(secondsBetween(agent.entered_at, state?.clock ?? null))}
                  </span>
                </span>
              </div>
            ))
          )}
        </div>
      </section>

      {(state?.open_episodes.length ?? 0) > 0 && (
        <section className="card">
          <header>
            <h2>Open conditions</h2>
            <p>true right now</p>
          </header>
          <div className="card-body">
            {state!.open_episodes.map((episode) => (
              <div className="stat-row" key={`${episode.rule_id}-${episode.subject_id}`}>
                <span className="label">
                  {episode.rule_name} · {episode.subject_id}
                </span>
                <span className="value">
                  {episode.fired_at ? "sent" : "waiting"}
                </span>
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="card">
        <header>
          <h2>Why it stayed quiet</h2>
          <p>suppressed, by reason</p>
        </header>
        <div className="card-body">
          {(suppressions?.by_reason.length ?? 0) === 0 ? (
            <p className="hint">Nothing suppressed yet.</p>
          ) : (
            suppressions!.by_reason.map((row) => (
              <div className="stat-row" key={row.reason}>
                <span className="label">{REASON_TEXT[row.reason] ?? row.reason}</span>
                <span className="value">{row.count}</span>
              </div>
            ))
          )}
        </div>
      </section>
    </aside>
  );
}

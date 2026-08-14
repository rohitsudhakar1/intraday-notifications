// Typed client for the FastAPI backend.
//
// The types here mirror the Pydantic models. They are hand-written rather than
// generated because there are eleven of them; past that I would generate from
// the OpenAPI schema FastAPI already publishes at /openapi.json.

export type Severity = "info" | "warning" | "critical";
export type Channel = "slack" | "email" | "in_app";
export type SubjectType = "queue" | "agent";
export type ScopeType = "all" | "ids" | "queues";
export type Operator = "gt" | "gte" | "lt" | "lte" | "eq";
export type NotificationKind = "alert" | "reminder" | "resolved" | "digest";

export interface User {
  id: string;
  name: string;
  role: "lead" | "agent";
  agent_id: string | null;
  slack_handle: string | null;
  email: string | null;
}

export interface Metric {
  key: string;
  label: string;
  subject_type: SubjectType;
  unit: "count" | "seconds" | "ratio";
  help_text: string;
  time_driven: boolean;
}

export interface Catalog {
  metrics: Metric[];
  operators: { key: Operator; label: string }[];
  queues: string[];
  agents: string[];
}

export interface Rule {
  id: number;
  name: string;
  enabled: boolean;
  subject_type: SubjectType;
  metric: string;
  operator: Operator;
  threshold: number;
  duration_sec: number;
  scope_type: ScopeType;
  scope_ids: string[];
  recipient_id: string;
  recipient_name: string | null;
  channel: Channel;
  severity: Severity;
  cooldown_sec: number;
  notify_on_resolve: boolean;
  created_at: string;
  summary: string;
}

export type RuleDraft = Omit<
  Rule,
  "id" | "created_at" | "summary" | "recipient_name"
>;

export interface Notification {
  id: number;
  rule_id: number;
  rule_name: string;
  subject_id: string;
  recipient_id: string;
  channel: Channel;
  severity: Severity;
  kind: NotificationKind;
  title: string;
  body: string;
  value: number | null;
  event_ts: string;
  created_at: string;
}

export interface QueueState {
  queue_id: string;
  tickets_waiting: number | null;
  longest_wait_sec: number | null;
  sla_target_sec: number | null;
  agents_available: number | null;
  agents_on_call: number | null;
  volume_last_15m: number | null;
  volume_forecast_next_15m: number | null;
  last_event_ts: string;
}

export interface AgentState {
  agent_id: string;
  state: string;
  entered_at: string;
  queue_ids: string[];
  in_violation: boolean;
  violation_started_at: string | null;
}

export interface OpenEpisode {
  rule_id: number;
  rule_name: string;
  subject_id: string;
  true_since: string;
  fired_at: string | null;
  last_value: number | null;
}

export interface WorldState {
  clock: string | null;
  queues: QueueState[];
  agents: AgentState[];
  open_episodes: OpenEpisode[];
}

export interface Suppressions {
  by_reason: { reason: string; count: number }[];
  recent: {
    id: number;
    rule_id: number | null;
    rule_name: string | null;
    subject_id: string | null;
    reason: string;
    detail: string | null;
    event_ts: string | null;
  }[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    // FastAPI returns validation failures as a structured list. Surfacing the
    // actual message matters here: the rule validator's errors are written to
    // be read by the person who made the mistake.
    const detail = await response.json().catch(() => null);
    throw new Error(formatError(detail) ?? `${response.status} ${response.statusText}`);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

function formatError(detail: unknown): string | null {
  if (!detail || typeof detail !== "object") return null;
  const body = (detail as { detail?: unknown }).detail;
  if (typeof body === "string") return body;
  if (Array.isArray(body)) {
    return body
      .map((item) => {
        const message = String((item as { msg?: string }).msg ?? "");
        return message.replace(/^Value error, /, "");
      })
      .filter(Boolean)
      .join(" · ");
  }
  return null;
}

export const api = {
  users: () => request<User[]>("/users"),
  catalog: () => request<Catalog>("/catalog"),
  state: () => request<WorldState>("/state"),
  rules: () => request<Rule[]>("/rules"),
  notifications: (recipientId?: string) =>
    request<Notification[]>(
      recipientId ? `/notifications?recipient_id=${recipientId}` : "/notifications",
    ),
  suppressions: () => request<Suppressions>("/suppressions"),
  createRule: (draft: RuleDraft) =>
    request<Rule>("/rules", { method: "POST", body: JSON.stringify(draft) }),
  updateRule: (id: number, patch: Partial<Rule>) =>
    request<Rule>(`/rules/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteRule: (id: number) => request<void>(`/rules/${id}`, { method: "DELETE" }),
};

// ------------------------------------------------------------------ display
// Formatting lives next to the types because every surface must phrase a
// duration the same way. The backend has the identical helpers for the text it
// writes into notifications.

export function humanizeSeconds(total: number): string {
  const seconds = Math.round(total);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) return remainder === 0 ? `${minutes}m` : `${minutes}m ${remainder}s`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 === 0 ? `${hours}h` : `${hours}h ${minutes % 60}m`;
}

export function formatValue(value: number | null, unit: Metric["unit"]): string {
  if (value === null || value === undefined) return "—";
  if (unit === "seconds") return humanizeSeconds(value);
  if (unit === "ratio") return `${Math.round(value * 100)}%`;
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

export function clockTime(iso: string | null): string {
  if (!iso) return "—";
  return iso.slice(11, 19);
}

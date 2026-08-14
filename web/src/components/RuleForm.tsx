import { useMemo, useState } from "react";
import { api, humanizeSeconds } from "../api";
import type { Catalog, Channel, Metric, Operator, RuleDraft, ScopeType, Severity } from "../api";

// The rule builder reads as one English sentence, because that is how the
// people who use it describe what they want: "tell me when the billing queue
// has more than 20 tickets waiting for 5 minutes."
//
// The form is generated from /api/catalog rather than hardcoded, so a new
// metric registered on the backend appears here with no frontend change.

const SEVERITIES: Severity[] = ["info", "warning", "critical"];
const CHANNELS: Channel[] = ["slack", "email", "in_app"];

const DURATIONS = [0, 60, 300, 600, 900, 1800, 2700, 3600];
const COOLDOWNS = [
  { value: 0, label: "never repeat until it resolves" },
  { value: 900, label: "remind me every 15m" },
  { value: 1800, label: "remind me every 30m" },
  { value: 3600, label: "remind me hourly" },
];

/** The threshold is stored in the metric's base unit, but nobody thinks in
 *  seconds or in fractions. Seconds are entered as minutes and ratios as
 *  percentages, which is what makes the "did you mean 0.8, not 80" mistake
 *  impossible to make from the UI at all -- the backend validator stays as the
 *  guard for anything posting to the API directly. */
function fromDisplay(value: number, unit: Metric["unit"]): number {
  if (unit === "seconds") return value * 60;
  if (unit === "ratio") return value / 100;
  return value;
}

function unitSuffix(unit: Metric["unit"]): string {
  return unit === "seconds" ? "minutes" : unit === "ratio" ? "%" : "";
}

export function RuleForm({
  catalog,
  me,
  onSaved,
  onCancel,
}: {
  catalog: Catalog;
  me: string;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [metricKey, setMetricKey] = useState(catalog.metrics[0]?.key ?? "");
  const metric = useMemo(
    () => catalog.metrics.find((entry) => entry.key === metricKey),
    [catalog.metrics, metricKey],
  );

  const [operator, setOperator] = useState<Operator>("gt");
  const [displayThreshold, setDisplayThreshold] = useState(20);
  const [durationSec, setDurationSec] = useState(300);
  const [scopeType, setScopeType] = useState<ScopeType>("all");
  const [scopeId, setScopeId] = useState("");
  const [severity, setSeverity] = useState<Severity>("warning");
  const [channel, setChannel] = useState<Channel>("slack");
  const [cooldownSec, setCooldownSec] = useState(1800);
  const [notifyOnResolve, setNotifyOnResolve] = useState(true);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const subjectType = metric?.subject_type ?? "queue";
  const scopeOptions = scopeType === "queues" || subjectType === "queue"
    ? catalog.queues
    : catalog.agents;

  function pickMetric(key: string) {
    setMetricKey(key);
    const next = catalog.metrics.find((entry) => entry.key === key);
    if (!next) return;
    // Switching between a queue metric and an agent metric invalidates the
    // scope, so reset it rather than letting a stale id be submitted.
    setScopeType("all");
    setScopeId("");
    setDisplayThreshold(next.unit === "ratio" ? 100 : next.unit === "seconds" ? 45 : 20);
  }

  async function save() {
    setError(null);
    if (!metric) return;
    setSaving(true);
    const draft: RuleDraft = {
      name: name.trim() || defaultName(metric, operator, displayThreshold),
      enabled: true,
      subject_type: subjectType,
      metric: metric.key,
      operator,
      threshold: fromDisplay(displayThreshold, metric.unit),
      duration_sec: durationSec,
      scope_type: scopeType,
      scope_ids: scopeType === "all" ? [] : scopeId ? [scopeId] : [],
      recipient_id: me,
      channel,
      severity,
      cooldown_sec: cooldownSec,
      notify_on_resolve: notifyOnResolve,
    };
    try {
      await api.createRule(draft);
      onSaved();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "could not save the rule");
    } finally {
      setSaving(false);
    }
  }

  if (!metric) return null;

  return (
    <div className="builder">
      <div className="sentence">
        <span>Notify me when</span>

        <select value={metricKey} onChange={(event) => pickMetric(event.target.value)}>
          <optgroup label="Queues">
            {catalog.metrics
              .filter((entry) => entry.subject_type === "queue")
              .map((entry) => (
                <option key={entry.key} value={entry.key}>
                  {entry.label}
                </option>
              ))}
          </optgroup>
          <optgroup label="Agents">
            {catalog.metrics
              .filter((entry) => entry.subject_type === "agent")
              .map((entry) => (
                <option key={entry.key} value={entry.key}>
                  {entry.label}
                </option>
              ))}
          </optgroup>
        </select>

        <span>for</span>

        <select
          value={scopeType}
          onChange={(event) => {
            setScopeType(event.target.value as ScopeType);
            setScopeId("");
          }}
        >
          <option value="all">{subjectType === "queue" ? "any queue" : "any agent"}</option>
          <option value="ids">{subjectType === "queue" ? "one queue" : "one agent"}</option>
          {subjectType === "agent" && <option value="queues">agents on a queue</option>}
        </select>

        {scopeType !== "all" && (
          <select value={scopeId} onChange={(event) => setScopeId(event.target.value)}>
            <option value="">choose…</option>
            {scopeOptions.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        )}

        <select value={operator} onChange={(event) => setOperator(event.target.value as Operator)}>
          {catalog.operators.map((entry) => (
            <option key={entry.key} value={entry.key}>
              {entry.label}
            </option>
          ))}
        </select>

        <input
          type="number"
          value={displayThreshold}
          min={0}
          step={metric.unit === "seconds" ? 0.5 : 1}
          onChange={(event) => setDisplayThreshold(Number(event.target.value))}
        />
        <span>{unitSuffix(metric.unit)}</span>

        <span>sustained for</span>
        <select value={durationSec} onChange={(event) => setDurationSec(Number(event.target.value))}>
          {DURATIONS.map((seconds) => (
            <option key={seconds} value={seconds}>
              {seconds === 0 ? "no wait — fire immediately" : humanizeSeconds(seconds)}
            </option>
          ))}
        </select>

        <span>· send as</span>
        <select value={severity} onChange={(event) => setSeverity(event.target.value as Severity)}>
          {SEVERITIES.map((entry) => (
            <option key={entry} value={entry}>
              {entry}
            </option>
          ))}
        </select>

        <span>via</span>
        <select value={channel} onChange={(event) => setChannel(event.target.value as Channel)}>
          {CHANNELS.map((entry) => (
            <option key={entry} value={entry}>
              {entry.replace("_", " ")}
            </option>
          ))}
        </select>

        <span>and</span>
        <select
          value={cooldownSec}
          onChange={(event) => setCooldownSec(Number(event.target.value))}
        >
          {COOLDOWNS.map((entry) => (
            <option key={entry.value} value={entry.value}>
              {entry.label}
            </option>
          ))}
        </select>
      </div>

      {metric.help_text && <p className="hint">{metric.help_text}</p>}

      {metric.time_driven && (
        <p className="hint">
          This is a duration that grows on its own, so it is checked on a timer rather
          than only when an event arrives. It will fire within 60s of the threshold.
        </p>
      )}

      <div className="preview">
        <b>What you will receive</b>
        {previewText(metric, operator, displayThreshold, durationSec, scopeType, scopeId)}
      </div>

      <div className="sentence">
        <span>Call it</span>
        <input
          type="text"
          placeholder={defaultName(metric, operator, displayThreshold)}
          value={name}
          onChange={(event) => setName(event.target.value)}
          style={{ minWidth: 260 }}
        />
        <label style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--ink-soft)" }}>
          <input
            type="checkbox"
            checked={notifyOnResolve}
            onChange={(event) => setNotifyOnResolve(event.target.checked)}
          />
          tell me when it recovers
        </label>
      </div>

      {error && <div className="form-error">{error}</div>}

      <div style={{ display: "flex", gap: 8 }}>
        <button className="primary" onClick={() => void save()} disabled={saving}>
          {saving ? "Saving…" : "Create rule"}
        </button>
        <button className="ghost" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function defaultName(metric: Metric, operator: Operator, threshold: number): string {
  const direction = operator.startsWith("lt") ? "below" : "over";
  return `${metric.label} ${direction} ${threshold}${unitSuffix(metric.unit) === "%" ? "%" : ""}`;
}

function previewText(
  metric: Metric,
  operator: Operator,
  threshold: number,
  durationSec: number,
  scopeType: ScopeType,
  scopeId: string,
): string {
  const subject =
    scopeType === "all"
      ? metric.subject_type === "queue" ? "any queue" : "any agent"
      : scopeType === "queues"
        ? `any agent on ${scopeId || "…"}`
        : scopeId || "…";
  const direction = operator.startsWith("lt") ? "drops below" : "goes above";
  const unit = unitSuffix(metric.unit);
  const window = durationSec === 0 ? "immediately" : `once it has held for ${humanizeSeconds(durationSec)}`;
  return `One message when ${metric.label.toLowerCase()} for ${subject} ${direction} ${threshold}${unit ? ` ${unit}` : ""}, sent ${window}. No repeats while it stays broken.`;
}

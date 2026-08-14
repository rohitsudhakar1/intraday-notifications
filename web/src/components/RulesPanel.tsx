import { useState } from "react";
import { api, humanizeSeconds } from "../api";
import type { Catalog, Rule } from "../api";
import { RuleForm } from "./RuleForm";

function routeText(rule: Rule): string {
  const scope =
    rule.scope_type === "all"
      ? rule.subject_type === "queue" ? "all queues" : "all agents"
      : rule.scope_type === "queues"
        ? `agents on ${rule.scope_ids.join(", ")}`
        : rule.scope_ids.join(", ");

  const repeat =
    rule.cooldown_sec === 0
      ? "once per incident"
      : `repeats every ${humanizeSeconds(rule.cooldown_sec)}`;

  return `${scope} · via ${rule.channel.replace("_", " ")} · ${repeat}${
    rule.notify_on_resolve ? " · says when it recovers" : ""
  }`;
}

export function RulesPanel({
  rules,
  catalog,
  me,
  onChanged,
}: {
  rules: Rule[];
  catalog: Catalog;
  me: string;
  onChanged: () => void;
}) {
  const [creating, setCreating] = useState(false);

  async function toggle(rule: Rule) {
    await api.updateRule(rule.id, { enabled: !rule.enabled });
    onChanged();
  }

  async function remove(rule: Rule) {
    await api.deleteRule(rule.id);
    onChanged();
  }

  return (
    <>
      <section className="card">
        <header>
          <h2>New rule</h2>
          <p>Describe it the way you would say it out loud.</p>
        </header>
        <div className="card-body">
          {creating ? (
            <RuleForm
              catalog={catalog}
              me={me}
              onSaved={() => {
                setCreating(false);
                onChanged();
              }}
              onCancel={() => setCreating(false)}
            />
          ) : (
            <button className="primary" onClick={() => setCreating(true)}>
              Create a rule
            </button>
          )}
        </div>
      </section>

      <section className="card">
        <header>
          <h2>My rules</h2>
          <p>{rules.filter((rule) => rule.enabled).length} active</p>
        </header>

        {rules.length === 0 ? (
          <p className="empty">No rules yet.</p>
        ) : (
          rules.map((rule) => (
            <div className="rule" key={rule.id} data-enabled={rule.enabled}>
              <div>
                <div className="rule-name">
                  {rule.name}
                  <span className="pill" data-tone={rule.severity}>
                    {rule.severity}
                  </span>
                  {!rule.enabled && (
                    <span className="pill" data-tone="muted">
                      paused
                    </span>
                  )}
                </div>
                <div className="rule-summary">{rule.summary}</div>
                <div className="rule-route">{routeText(rule)}</div>
              </div>

              <div className="rule-actions">
                <button className="ghost" onClick={() => void toggle(rule)}>
                  {rule.enabled ? "Pause" : "Resume"}
                </button>
                <button className="ghost" data-danger="true" onClick={() => void remove(rule)}>
                  Delete
                </button>
              </div>
            </div>
          ))
        )}
      </section>
    </>
  );
}

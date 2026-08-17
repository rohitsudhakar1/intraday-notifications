import { useCallback, useEffect, useState } from "react";
import { api, clockTime } from "./api";
import type { Catalog, Notification, Rule, Suppressions, User, WorldState } from "./api";
import { Inbox } from "./components/Inbox";
import { RulesPanel } from "./components/RulesPanel";
import { Sidebar } from "./components/Sidebar";

// There is no auth, so "who am I" is a picker. That is not a shortcut: it is
// the fastest way to demonstrate that routing works, because you can watch the
// same morning from a lead's seat and from an agent's seat.
const POLL_MS = 2000;

type Tab = "inbox" | "rules";

export function App() {
  const [users, setUsers] = useState<User[]>([]);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [me, setMe] = useState<string>("");
  const [tab, setTab] = useState<Tab>("inbox");

  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [rules, setRules] = useState<Rule[]>([]);
  const [state, setState] = useState<WorldState | null>(null);
  const [suppressions, setSuppressions] = useState<Suppressions | null>(null);

  useEffect(() => {
    void (async () => {
      const [loadedUsers, loadedCatalog] = await Promise.all([api.users(), api.catalog()]);
      setUsers(loadedUsers);
      setCatalog(loadedCatalog);
      setMe((current) => current || loadedUsers[0]?.id || "");
    })();
  }, []);

  const refresh = useCallback(async () => {
    if (!me) return;
    const [feed, allRules, world, quiet] = await Promise.all([
      api.notifications(me),
      api.rules(),
      api.state(),
      api.suppressions(),
    ]);
    setNotifications(feed);
    setRules(allRules);
    setState(world);
    setSuppressions(quiet);
  }, [me]);

  // Poll rather than push. A websocket is the right answer in production, but
  // it would be the only piece of infrastructure here that exists purely for
  // the demo, and polling makes the replay just as legible.
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  // The catalog is rebuilt on refresh so queues and agents discovered during a
  // replay become selectable in the rule builder without a reload.
  useEffect(() => {
    if (!state) return;
    void api.catalog().then(setCatalog);
  }, [state?.queues.length, state?.agents.length]);

  const currentUser = users.find((user) => user.id === me);
  const myRules = rules.filter((rule) => rule.recipient_id === me);

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          Intraday <span>/ notifications</span>
        </div>

        <div className="clock">
          feed clock <b>{clockTime(state?.clock ?? null)}</b>
        </div>

        <nav className="tabs">
          <button data-active={tab === "inbox"} onClick={() => setTab("inbox")}>
            Inbox {notifications.length > 0 && `(${notifications.length})`}
          </button>
          <button data-active={tab === "rules"} onClick={() => setTab("rules")}>
            Rules ({myRules.length})
          </button>
        </nav>

        <div className="spacer" />

        <div className="who">
          <label htmlFor="who">Viewing as</label>
          <select id="who" value={me} onChange={(event) => setMe(event.target.value)}>
            {users.map((user) => (
              <option key={user.id} value={user.id}>
                {user.name} · {user.role}
              </option>
            ))}
          </select>
        </div>
      </header>

      <div className="columns">
        <main>
          {tab === "inbox" ? (
            <Inbox
              notifications={notifications}
              user={currentUser}
              ruleCount={myRules.filter((rule) => rule.enabled).length}
              feedHasRun={state !== null && state.queues.length > 0}
              openEpisodes={state?.open_episodes ?? []}
            />
          ) : (
            catalog && (
              <RulesPanel
                rules={myRules}
                catalog={catalog}
                me={me}
                onChanged={() => void refresh()}
              />
            )
          )}
        </main>

        <Sidebar state={state} suppressions={suppressions} />
      </div>
    </div>
  );
}

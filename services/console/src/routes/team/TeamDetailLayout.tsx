import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet, useParams } from "react-router-dom";
import { getTeam } from "../../api/teams";
import { ErrorNotice } from "../../components/ErrorNotice";
import { RebuildDialog } from "../../components/RebuildDialog";
import styles from "../../components/ui.module.css";

const TABS = [
  { to: "knowledge", label: "Knowledge" },
  { to: "wisdom", label: "Wisdom" },
  { to: "memory", label: "Memory" },
  { to: "commit-log", label: "Commit log" },
  { to: "proposals", label: "Proposals" },
  { to: "code", label: "Code" },
  { to: "keys", label: "Keys" },
];

export function TeamDetailLayout() {
  const { team = "" } = useParams<{ team: string }>();
  const [rebuilding, setRebuilding] = useState(false);
  const { data, error } = useQuery({
    queryKey: ["admin", "teams", team],
    queryFn: () => getTeam(team),
  });

  return (
    <div>
      <div className={styles.actionRow}>
        <h1>{data?.display_name || team}</h1>
        <button type="button" className={styles.buttonSecondary} onClick={() => setRebuilding(true)}>
          Rebuild
        </button>
      </div>
      {error && <ErrorNotice error={error} />}
      {data && (
        <p>
          status: {data.status ?? "-"} | graph: <code>{data.graph}</code> | code graph:{" "}
          <code>{data.code_graph}</code>
        </p>
      )}
      <nav className={styles.tabNav}>
        {TABS.map((tab) => (
          <NavLink key={tab.to} to={tab.to} className={({ isActive }) => (isActive ? styles.active : undefined)}>
            {tab.label}
          </NavLink>
        ))}
      </nav>
      <Outlet />

      {rebuilding && <RebuildDialog team={team} onClose={() => setRebuilding(false)} />}
    </div>
  );
}

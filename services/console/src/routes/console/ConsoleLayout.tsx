import { NavLink, Outlet } from "react-router-dom";
import styles from "../../components/ui.module.css";

const TABS = [
  { to: "operators", label: "Operators" },
  { to: "keys", label: "Keys" },
  { to: "audit", label: "Audit log" },
  { to: "jobs", label: "Jobs" },
];

export function ConsoleLayout() {
  return (
    <div>
      <h1>Console administration</h1>
      <nav className={styles.tabNav}>
        {TABS.map((tab) => (
          <NavLink key={tab.to} to={tab.to} className={({ isActive }) => (isActive ? styles.active : undefined)}>
            {tab.label}
          </NavLink>
        ))}
      </nav>
      <Outlet />
    </div>
  );
}

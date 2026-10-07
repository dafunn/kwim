import { NavLink, Outlet } from "react-router-dom";
import styles from "../../components/ui.module.css";

const SUB_TABS = [
  { to: "semantic", label: "Semantic" },
  { to: "episodic", label: "Episodic" },
  { to: "working", label: "Working" },
];

export function Memory() {
  return (
    <div>
      <nav className={styles.tabNav}>
        {SUB_TABS.map((tab) => (
          <NavLink key={tab.to} to={tab.to} className={({ isActive }) => (isActive ? styles.active : undefined)}>
            {tab.label}
          </NavLink>
        ))}
      </nav>
      <Outlet />
    </div>
  );
}

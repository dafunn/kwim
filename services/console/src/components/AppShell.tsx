import { useMutation, useQueryClient } from "@tanstack/react-query";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { logout } from "../auth/session";
import styles from "./ui.module.css";

export function AppShell() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const logoutMutation = useMutation({
    mutationFn: logout,
    onSettled: () => {
      // Clear local state whether or not DELETE succeeded.
      queryClient.clear();
      navigate("/login", { replace: true });
    },
  });

  return (
    <div>
      <header className={styles.appHeader}>
        <span className={styles.brand}>KWIM Admin</span>
        <nav className={styles.appNav}>
          <NavLink to="/teams" className={({ isActive }) => (isActive ? styles.active : undefined)}>
            Teams
          </NavLink>
          <NavLink to="/console" className={({ isActive }) => (isActive ? styles.active : undefined)}>
            Console administration
          </NavLink>
        </nav>
        <button type="button" className={styles.buttonSecondary} onClick={() => logoutMutation.mutate()} disabled={logoutMutation.isPending}>
          {logoutMutation.isPending ? "Logging out..." : "Log out"}
        </button>
      </header>
      <div className={styles.appBody}>
        <Outlet />
      </div>
    </div>
  );
}

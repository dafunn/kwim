import type { ReactNode } from "react";
import styles from "./ui.module.css";

interface ObjectDrawerProps {
  title: string;
  onClose: () => void;
  children: ReactNode;
}

/** Opens over any fact, rule, or semantic item row. Read-only. */
export function ObjectDrawer({ title, onClose, children }: ObjectDrawerProps) {
  return (
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.panel} onClick={(e) => e.stopPropagation()} role="dialog" aria-label={title}>
        <div className={styles.panelHeader}>
          <h2>{title}</h2>
          <button type="button" className={styles.closeButton} onClick={onClose} aria-label="Close">
            x
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function DrawerSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className={styles.section}>
      <h3>{title}</h3>
      {children}
    </div>
  );
}

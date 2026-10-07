import type { ReactNode } from "react";
import styles from "./ui.module.css";

interface ModalProps {
  title: string;
  onClose: () => void;
  children: ReactNode;
}

export function Modal({ title, onClose, children }: ModalProps) {
  return (
    <div className={styles.modalOverlay} onClick={onClose}>
      <div className={styles.modal} onClick={(e) => e.stopPropagation()} role="dialog" aria-label={title}>
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

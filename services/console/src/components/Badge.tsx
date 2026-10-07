import type { ReactNode } from "react";
import styles from "./ui.module.css";

type Tone = "good" | "bad" | "warn" | "neutral";

interface BadgeProps {
  tone?: Tone;
  children: ReactNode;
  title?: string;
}

export function Badge({ tone = "neutral", children, title }: BadgeProps) {
  return (
    <span className={`${styles.badge} ${styles[tone]}`} title={title}>
      {children}
    </span>
  );
}

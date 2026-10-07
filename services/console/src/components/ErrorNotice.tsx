import { describeError } from "../api/describeError";
import styles from "./ui.module.css";

export function ErrorNotice({ error }: { error: unknown }) {
  return (
    <p className={`${styles.badge} ${styles.bad}`} role="alert">
      {describeError(error)}
    </p>
  );
}

import styles from "./ui.module.css";

interface JsonViewerProps {
  value: unknown;
  label?: string;
  defaultOpen?: boolean;
}

/** Payloads and provenance render in a collapsible viewer, not a text area. */
export function JsonViewer({ value, label = "payload", defaultOpen = false }: JsonViewerProps) {
  return (
    <details className={styles.json} open={defaultOpen}>
      <summary>{label}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

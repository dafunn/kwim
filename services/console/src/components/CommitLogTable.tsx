import type { CommitLogRow } from "../api/commitLog";
import { Badge } from "./Badge";
import { DataTable, type Column } from "./DataTable";
import { JsonViewer } from "./JsonViewer";
import { Timestamp } from "./Timestamp";
import styles from "./ui.module.css";

const OPERATION_TONE: Record<string, "good" | "bad" | "warn" | "neutral"> = {
  commit: "good",
  confirm: "good",
  reinforce: "good",
  amend: "warn",
  deprecate: "warn",
  retract: "bad",
};

/** For an amend row, provenance.previous_payload next to payload is the only
 * place an edit's before/after survives. */
function PayloadCell({ row }: { row: CommitLogRow }) {
  const previous = row.operation === "amend" ? row.provenance.previous_payload : undefined;
  if (previous) {
    return (
      <div className={styles.diffPair}>
        <div>
          <h4>previous_payload</h4>
          <JsonViewer value={previous} label="before" defaultOpen />
        </div>
        <div>
          <h4>payload</h4>
          <JsonViewer value={row.payload} label="after" defaultOpen />
        </div>
      </div>
    );
  }
  return <JsonViewer value={row.payload} label="payload" />;
}

export function CommitLogTable({ rows }: { rows: CommitLogRow[] }) {
  const columns: Column<CommitLogRow>[] = [
    { key: "seq", header: "Seq", render: (r) => r.seq },
    { key: "committed_at", header: "Committed", render: (r) => <Timestamp value={r.committed_at} /> },
    { key: "object_type", header: "Type", render: (r) => r.object_type },
    { key: "object_id", header: "Object", render: (r) => <code>{r.object_id}</code> },
    {
      key: "operation",
      header: "Operation",
      render: (r) => <Badge tone={OPERATION_TONE[r.operation] ?? "neutral"}>{r.operation}</Badge>,
    },
    { key: "source_kind", header: "Source", render: (r) => r.source_kind ?? "-" },
    { key: "gate_decision", header: "Gate", render: (r) => r.gate_decision ?? "-" },
    { key: "proposed_by", header: "Proposed by", render: (r) => r.proposed_by ?? "-" },
    { key: "payload", header: "Payload", render: (r) => <PayloadCell row={r} /> },
  ];

  return (
    <DataTable columns={columns} rows={rows} getRowKey={(r) => String(r.seq)} emptyMessage="No commit-log rows." />
  );
}

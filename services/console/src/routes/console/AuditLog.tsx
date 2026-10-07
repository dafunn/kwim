import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { AuditFilters, AuditLogRow } from "../../api/audit";
import { listAudit } from "../../api/audit";
import { listOperators } from "../../api/operators";
import { useCursorList } from "../../api/useCursorList";
import { Badge } from "../../components/Badge";
import { type Column, DataTable } from "../../components/DataTable";
import { ErrorNotice } from "../../components/ErrorNotice";
import { JsonViewer } from "../../components/JsonViewer";
import { LoadMore } from "../../components/LoadMore";
import { Timestamp } from "../../components/Timestamp";
import styles from "../../components/ui.module.css";

const RESULT_TONE: Record<string, "good" | "bad" | "warn"> = {
  ok: "good",
  denied: "warn",
  error: "bad",
};

/** The system of record for every mutation (admin_write.py's `audited`) - the read side of it. */
export function AuditLog() {
  const [filters, setFilters] = useState<AuditFilters>({});
  const [draft, setDraft] = useState<AuditFilters>({});

  const operators = useQuery({ queryKey: ["admin", "operators"], queryFn: listOperators });
  const usernameById = new Map((operators.data?.items ?? []).map((o) => [o.id, o.username]));

  const { items, isLoading, error, hasNextPage, isFetchingNextPage, fetchNextPage } = useCursorList<AuditLogRow>(
    ["admin", "audit", filters],
    (cursor) => listAudit(filters, cursor),
  );

  const columns: Column<AuditLogRow>[] = [
    { key: "at", header: "At", render: (r) => <Timestamp value={r.at} /> },
    { key: "action", header: "Action", render: (r) => r.action },
    {
      key: "result",
      header: "Result",
      render: (r) => <Badge tone={RESULT_TONE[r.result] ?? "neutral"}>{r.result}</Badge>,
    },
    { key: "operator_id", header: "Operator", render: (r) => (r.operator_id ? usernameById.get(r.operator_id) ?? r.operator_id : "-") },
    { key: "team", header: "Team", render: (r) => r.team ?? "-" },
    { key: "object", header: "Object", render: (r) => (r.object_type ? `${r.object_type}:${r.object_id ?? "?"}` : "-") },
    { key: "detail", header: "Detail", render: (r) => <JsonViewer value={r.detail} label="detail" /> },
  ];

  return (
    <div>
      <form
        className={styles.filterBar}
        onSubmit={(e) => {
          e.preventDefault();
          setFilters(draft);
        }}
      >
        <div className={styles.filterField}>
          <label htmlFor="audit-team">Team</label>
          <input id="audit-team" value={draft.team ?? ""} onChange={(e) => setDraft({ ...draft, team: e.target.value || undefined })} />
        </div>
        <div className={styles.filterField}>
          <label htmlFor="audit-operator">Operator</label>
          <select
            id="audit-operator"
            value={draft.operator ?? ""}
            onChange={(e) => setDraft({ ...draft, operator: e.target.value || undefined })}
          >
            <option value="">any</option>
            {(operators.data?.items ?? []).map((o) => (
              <option key={o.id} value={o.id}>
                {o.username}
              </option>
            ))}
          </select>
        </div>
        <div className={styles.filterField}>
          <label htmlFor="audit-action">Action</label>
          <input
            id="audit-action"
            value={draft.action ?? ""}
            onChange={(e) => setDraft({ ...draft, action: e.target.value || undefined })}
            placeholder="e.g. fact.amend"
          />
        </div>
        <button type="submit">Apply</button>
      </form>

      {isLoading && <p>Loading audit log...</p>}
      {error && <ErrorNotice error={error} />}
      <DataTable columns={columns} rows={items} getRowKey={(r) => String(r.seq)} emptyMessage="No audit rows." />
      <LoadMore hasNextPage={hasNextPage} isFetchingNextPage={isFetchingNextPage} onClick={() => fetchNextPage()} />
    </div>
  );
}

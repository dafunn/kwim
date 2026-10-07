import { useState } from "react";
import { useParams } from "react-router-dom";
import type { Fact, FactFilters } from "../../api/facts";
import { listFacts } from "../../api/facts";
import { useCursorList } from "../../api/useCursorList";
import { CreateFactDialog } from "../../components/CreateFactDialog";
import { type Column, DataTable } from "../../components/DataTable";
import { ErrorNotice } from "../../components/ErrorNotice";
import { FactDrawer } from "../../components/FactDrawer";
import { Freshness } from "../../components/Freshness";
import { LoadMore } from "../../components/LoadMore";
import { Timestamp } from "../../components/Timestamp";
import styles from "../../components/ui.module.css";

const STATUS_OPTIONS = ["any", "current", "superseded", "retracted"];

export function Knowledge() {
  const { team = "" } = useParams<{ team: string }>();
  const [filters, setFilters] = useState<FactFilters>({ status: "any" });
  const [draft, setDraft] = useState<FactFilters>({ status: "any" });
  const [openFactId, setOpenFactId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const { items, isLoading, error, hasNextPage, isFetchingNextPage, fetchNextPage } = useCursorList<Fact>(
    ["admin", "teams", team, "facts", filters],
    (cursor) => listFacts(team, filters, cursor),
  );

  const columns: Column<Fact>[] = [
    { key: "statement", header: "Statement", render: (f) => f.statement },
    { key: "fact_type", header: "Type", render: (f) => f.fact_type },
    { key: "status", header: "Status", render: (f) => f.status },
    { key: "freshness", header: "Freshness", render: (f) => <Freshness band={f.freshness} asOf={f.as_of} /> },
    { key: "source_kind", header: "Source", render: (f) => f.source_kind ?? "-" },
    { key: "created_at", header: "Created", render: (f) => <Timestamp value={f.created_at} /> },
  ];

  return (
    <div>
      <div className={styles.actionRow}>
        <button type="button" className={styles.buttonPrimary} onClick={() => setCreating(true)}>
          New fact
        </button>
      </div>
      <form
        className={styles.filterBar}
        onSubmit={(e) => {
          e.preventDefault();
          setFilters(draft);
        }}
      >
        <div className={styles.filterField}>
          <label htmlFor="fact-status">Status</label>
          <select
            id="fact-status"
            value={draft.status}
            onChange={(e) => setDraft({ ...draft, status: e.target.value })}
          >
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <div className={styles.filterField}>
          <label htmlFor="fact-type">Fact type</label>
          <input
            id="fact-type"
            value={draft.fact_type ?? ""}
            onChange={(e) => setDraft({ ...draft, fact_type: e.target.value || undefined })}
          />
        </div>
        <div className={styles.filterField}>
          <label htmlFor="fact-source">Source kind</label>
          <input
            id="fact-source"
            value={draft.source_kind ?? ""}
            onChange={(e) => setDraft({ ...draft, source_kind: e.target.value || undefined })}
          />
        </div>
        <div className={styles.filterField}>
          <label htmlFor="fact-about">About</label>
          <input
            id="fact-about"
            placeholder="comma-separated"
            onChange={(e) =>
              setDraft({
                ...draft,
                about: e.target.value ? e.target.value.split(",").map((s) => s.trim()) : undefined,
              })
            }
          />
        </div>
        <div className={styles.filterField}>
          <label htmlFor="fact-q">Statement contains</label>
          <input id="fact-q" value={draft.q ?? ""} onChange={(e) => setDraft({ ...draft, q: e.target.value || undefined })} />
        </div>
        <button type="submit">Apply</button>
      </form>

      {isLoading && <p>Loading facts...</p>}
      {error && <ErrorNotice error={error} />}
      <DataTable
        columns={columns}
        rows={items}
        getRowKey={(f) => f.id}
        onRowClick={(f) => setOpenFactId(f.id)}
        emptyMessage="No facts match these filters."
      />
      <LoadMore hasNextPage={hasNextPage} isFetchingNextPage={isFetchingNextPage} onClick={() => fetchNextPage()} />

      {openFactId && <FactDrawer team={team} factId={openFactId} onClose={() => setOpenFactId(null)} />}
      {creating && <CreateFactDialog team={team} onClose={() => setCreating(false)} />}
    </div>
  );
}

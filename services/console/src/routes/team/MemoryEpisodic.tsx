import { useState } from "react";
import { useParams } from "react-router-dom";
import type { EpisodicEvent, EpisodicFilters } from "../../api/episodic";
import { listEpisodic } from "../../api/episodic";
import { useCursorList } from "../../api/useCursorList";
import { Badge } from "../../components/Badge";
import { type Column, DataTable } from "../../components/DataTable";
import { ErrorNotice } from "../../components/ErrorNotice";
import { JsonViewer } from "../../components/JsonViewer";
import { LoadMore } from "../../components/LoadMore";
import { Timestamp } from "../../components/Timestamp";
import styles from "../../components/ui.module.css";

const DEFAULT_FILTERS: EpisodicFilters = { archived: "false", order: "desc" };

export function MemoryEpisodic() {
  const { team = "" } = useParams<{ team: string }>();
  const [filters, setFilters] = useState<EpisodicFilters>(DEFAULT_FILTERS);
  const [draft, setDraft] = useState<EpisodicFilters>(DEFAULT_FILTERS);

  const { items, isLoading, error, hasNextPage, isFetchingNextPage, fetchNextPage } = useCursorList<EpisodicEvent>(
    ["admin", "teams", team, "episodic", filters],
    (cursor) => listEpisodic(team, filters, cursor),
  );

  const columns: Column<EpisodicEvent>[] = [
    { key: "occurred_at", header: "Occurred", render: (e) => <Timestamp value={e.occurred_at} /> },
    { key: "event_type", header: "Event type", render: (e) => e.event_type },
    { key: "agent_id", header: "Agent", render: (e) => e.agent_id },
    { key: "session_id", header: "Session", render: (e) => <code>{e.session_id}</code> },
    { key: "archived", header: "Archived", render: (e) => (e.archived ? <Badge tone="warn">archived</Badge> : "-") },
    { key: "event_data", header: "Data", render: (e) => <JsonViewer value={e.event_data} label="event_data" /> },
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
          <label htmlFor="ep-type">Event type</label>
          <input
            id="ep-type"
            value={draft.event_type ?? ""}
            onChange={(e) => setDraft({ ...draft, event_type: e.target.value || undefined })}
          />
        </div>
        <div className={styles.filterField}>
          <label htmlFor="ep-agent">Agent id</label>
          <input
            id="ep-agent"
            value={draft.agent_id ?? ""}
            onChange={(e) => setDraft({ ...draft, agent_id: e.target.value || undefined })}
          />
        </div>
        <div className={styles.filterField}>
          <label htmlFor="ep-archived">Archived</label>
          <select
            id="ep-archived"
            value={draft.archived}
            onChange={(e) => setDraft({ ...draft, archived: e.target.value as EpisodicFilters["archived"] })}
          >
            <option value="false">not archived</option>
            <option value="true">archived</option>
            <option value="any">any</option>
          </select>
        </div>
        <div className={styles.filterField}>
          <label htmlFor="ep-order">Order</label>
          <select
            id="ep-order"
            value={draft.order}
            onChange={(e) => setDraft({ ...draft, order: e.target.value as EpisodicFilters["order"] })}
          >
            <option value="desc">newest first</option>
            <option value="asc">oldest first</option>
          </select>
        </div>
        <button type="submit">Apply</button>
      </form>

      {isLoading && <p>Loading episodic events...</p>}
      {error && <ErrorNotice error={error} />}
      <DataTable columns={columns} rows={items} getRowKey={(e) => e.id} emptyMessage="No episodic events." />
      <LoadMore hasNextPage={hasNextPage} isFetchingNextPage={isFetchingNextPage} onClick={() => fetchNextPage()} />
    </div>
  );
}

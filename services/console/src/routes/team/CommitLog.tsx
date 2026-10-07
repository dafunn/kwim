import { useState } from "react";
import { useParams } from "react-router-dom";
import type { CommitLogFilters, CommitLogRow } from "../../api/commitLog";
import { listCommitLog } from "../../api/commitLog";
import { useCursorList } from "../../api/useCursorList";
import { CommitLogTable } from "../../components/CommitLogTable";
import { ErrorNotice } from "../../components/ErrorNotice";
import { LoadMore } from "../../components/LoadMore";
import styles from "../../components/ui.module.css";

const OPERATIONS = ["commit", "deprecate", "reinforce", "retract", "confirm", "amend"];
const OBJECT_TYPES = ["fact", "rule", "semantic"];
const GATE_DECISIONS = ["auto_committed", "human_approved", "human_retracted", "human_confirmed"];

const DEFAULT_FILTERS: CommitLogFilters = { order: "desc" };

/** The system of record - reads like one: seq-ordered, full payload and provenance per row. */
export function CommitLog() {
  const { team = "" } = useParams<{ team: string }>();
  const [filters, setFilters] = useState<CommitLogFilters>(DEFAULT_FILTERS);
  const [draft, setDraft] = useState<CommitLogFilters>(DEFAULT_FILTERS);

  const { items, isLoading, error, hasNextPage, isFetchingNextPage, fetchNextPage } = useCursorList<CommitLogRow>(
    ["admin", "teams", team, "commit-log", filters],
    (cursor) => listCommitLog(team, filters, cursor),
  );

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
          <label htmlFor="cl-object-id">Object id</label>
          <input
            id="cl-object-id"
            value={draft.object_id ?? ""}
            onChange={(e) => setDraft({ ...draft, object_id: e.target.value || undefined })}
          />
        </div>
        <div className={styles.filterField}>
          <label htmlFor="cl-object-type">Object type</label>
          <select
            id="cl-object-type"
            value={draft.object_type ?? ""}
            onChange={(e) => setDraft({ ...draft, object_type: e.target.value || undefined })}
          >
            <option value="">any</option>
            {OBJECT_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
        <div className={styles.filterField}>
          <label htmlFor="cl-operation">Operation</label>
          <select
            id="cl-operation"
            value={draft.operation ?? ""}
            onChange={(e) => setDraft({ ...draft, operation: e.target.value || undefined })}
          >
            <option value="">any</option>
            {OPERATIONS.map((op) => (
              <option key={op} value={op}>
                {op}
              </option>
            ))}
          </select>
        </div>
        <div className={styles.filterField}>
          <label htmlFor="cl-gate">Gate decision</label>
          <select
            id="cl-gate"
            value={draft.gate_decision ?? ""}
            onChange={(e) => setDraft({ ...draft, gate_decision: e.target.value || undefined })}
          >
            <option value="">any</option>
            {GATE_DECISIONS.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </select>
        </div>
        <div className={styles.filterField}>
          <label htmlFor="cl-order">Order</label>
          <select
            id="cl-order"
            value={draft.order}
            onChange={(e) => setDraft({ ...draft, order: e.target.value as CommitLogFilters["order"] })}
          >
            <option value="desc">newest first</option>
            <option value="asc">oldest first</option>
          </select>
        </div>
        <button type="submit">Apply</button>
      </form>

      {isLoading && <p>Loading commit log...</p>}
      {error && <ErrorNotice error={error} />}
      <CommitLogTable rows={items} />
      <LoadMore hasNextPage={hasNextPage} isFetchingNextPage={isFetchingNextPage} onClick={() => fetchNextPage()} />
    </div>
  );
}

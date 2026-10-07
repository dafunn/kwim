import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { listWorking, type WorkingKey } from "../../api/working";
import { type Column, DataTable } from "../../components/DataTable";
import { ErrorNotice } from "../../components/ErrorNotice";
import styles from "../../components/ui.module.css";

/** Diagnostic only - shows TTLs, never values; ephemeral and not rebuilt. */
export function MemoryWorking() {
  const { team = "" } = useParams<{ team: string }>();
  const [session, setSession] = useState("");
  const [appliedSession, setAppliedSession] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "teams", team, "working", appliedSession],
    queryFn: () => listWorking(team, appliedSession),
    enabled: appliedSession.length > 0,
  });

  const columns: Column<WorkingKey>[] = [
    { key: "key", header: "Key", render: (k) => <code>{k.key}</code> },
    { key: "ttl_seconds", header: "TTL (seconds)", render: (k) => (k.ttl_seconds ?? "no expiry").toString() },
  ];

  return (
    <div>
      <p>Ephemeral working-memory keys for one session. Not rebuilt on a rebuild - only the commit log is replayed.</p>
      <form
        className={styles.filterBar}
        onSubmit={(e) => {
          e.preventDefault();
          setAppliedSession(session);
        }}
      >
        <div className={styles.filterField}>
          <label htmlFor="working-session">Session id</label>
          <input id="working-session" value={session} onChange={(e) => setSession(e.target.value)} required />
        </div>
        <button type="submit">Load</button>
      </form>

      {isLoading && <p>Loading...</p>}
      {error && <ErrorNotice error={error} />}
      {data && (
        <DataTable
          columns={columns}
          rows={data.items}
          getRowKey={(k) => k.key}
          emptyMessage="No working-memory keys for this session."
        />
      )}
    </div>
  );
}

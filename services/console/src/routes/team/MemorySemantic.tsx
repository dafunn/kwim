import { useState } from "react";
import { useParams } from "react-router-dom";
import type { SemanticItem } from "../../api/semantic";
import { listSemantic } from "../../api/semantic";
import { useCursorList } from "../../api/useCursorList";
import { AmendSemanticDialog } from "../../components/AmendSemanticDialog";
import { CreateSemanticDialog } from "../../components/CreateSemanticDialog";
import { type Column, DataTable } from "../../components/DataTable";
import { ErrorNotice } from "../../components/ErrorNotice";
import { JsonViewer } from "../../components/JsonViewer";
import { LoadMore } from "../../components/LoadMore";
import { DrawerSection, ObjectDrawer } from "../../components/ObjectDrawer";
import styles from "../../components/ui.module.css";

/** Listable with no query - the team-key API can't do this at all. `q` switches to vector search, which the server never paginates. */
export function MemorySemantic() {
  const { team = "" } = useParams<{ team: string }>();
  const [q, setQ] = useState("");
  const [appliedQ, setAppliedQ] = useState("");
  const [open, setOpen] = useState<SemanticItem | null>(null);
  const [amending, setAmending] = useState(false);
  const [creating, setCreating] = useState(false);

  const { items, isLoading, error, hasNextPage, isFetchingNextPage, fetchNextPage } = useCursorList<SemanticItem>(
    ["admin", "teams", team, "semantic", appliedQ],
    (cursor) => listSemantic(team, { q: appliedQ || undefined, cursor }),
  );

  const columns: Column<SemanticItem>[] = [
    { key: "content", header: "Content", render: (item) => item.content.slice(0, 160) },
    { key: "id", header: "Id", render: (item) => <code>{item.id}</code> },
    ...(appliedQ ? [{ key: "score", header: "Distance", render: (item: SemanticItem) => item.score.toFixed(4) }] : []),
  ];

  return (
    <div>
      <div className={styles.actionRow}>
        <button type="button" className={styles.buttonPrimary} onClick={() => setCreating(true)}>
          New semantic item
        </button>
      </div>
      <form
        className={styles.filterBar}
        onSubmit={(e) => {
          e.preventDefault();
          setAppliedQ(q);
        }}
      >
        <div className={styles.filterField}>
          <label htmlFor="semantic-q">Search (optional)</label>
          <input id="semantic-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="vector search" />
        </div>
        <button type="submit">Search</button>
        {appliedQ && (
          <button
            type="button"
            onClick={() => {
              setQ("");
              setAppliedQ("");
            }}
          >
            Clear
          </button>
        )}
      </form>

      {isLoading && <p>Loading semantic items...</p>}
      {error && <ErrorNotice error={error} />}
      <DataTable
        columns={columns}
        rows={items}
        getRowKey={(item) => item.id}
        onRowClick={setOpen}
        emptyMessage="No semantic items."
      />
      {!appliedQ && (
        <LoadMore hasNextPage={hasNextPage} isFetchingNextPage={isFetchingNextPage} onClick={() => fetchNextPage()} />
      )}

      {open && (
        <ObjectDrawer title="Semantic item" onClose={() => setOpen(null)}>
          <DrawerSection title="Content">
            <p>{open.content}</p>
          </DrawerSection>
          <div className={styles.actionRow}>
            <button type="button" className={styles.buttonSecondary} onClick={() => setAmending(true)}>
              Amend
            </button>
          </div>
          <DrawerSection title="Metadata">
            <JsonViewer value={open.metadata} label="metadata" defaultOpen />
          </DrawerSection>
        </ObjectDrawer>
      )}

      {open && amending && (
        <AmendSemanticDialog
          team={team}
          item={open}
          onClose={() => setAmending(false)}
          onSuccess={() => {
            setAmending(false);
            setOpen(null);
          }}
        />
      )}

      {creating && <CreateSemanticDialog team={team} onClose={() => setCreating(false)} />}
    </div>
  );
}

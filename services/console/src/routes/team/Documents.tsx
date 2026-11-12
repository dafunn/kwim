import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { listDocuments, type DocumentSummary } from "../../api/documents";
import { Badge } from "../../components/Badge";
import { type Column, DataTable } from "../../components/DataTable";
import { DocumentDialog } from "../../components/DocumentDialog";
import { DocumentUploadDialog } from "../../components/DocumentUploadDialog";
import { ErrorNotice } from "../../components/ErrorNotice";
import { Timestamp } from "../../components/Timestamp";
import { statusTone } from "../../components/documentStatus";
import styles from "../../components/ui.module.css";

export function Documents() {
  const { team = "" } = useParams<{ team: string }>();
  const [uploading, setUploading] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "teams", team, "documents"],
    queryFn: () => listDocuments(team),
    refetchInterval: (q) => (q.state.data?.items.some((d) => d.status === "ingesting") ? 2000 : false),
  });

  const columns: Column<DocumentSummary>[] = [
    { key: "title", header: "Title", render: (d) => d.title },
    { key: "status", header: "Status", render: (d) => <Badge tone={statusTone(d.status)}>{d.status}</Badge> },
    { key: "tags", header: "Tags", render: (d) => d.tags.join(", ") || "-" },
    { key: "created_by", header: "Uploaded by", render: (d) => d.created_by ?? "-" },
    { key: "updated_at", header: "Updated", render: (d) => <Timestamp value={d.updated_at} /> },
  ];

  return (
    <div>
      <div className={styles.actionRow}>
        <button type="button" className={styles.buttonPrimary} onClick={() => setUploading(true)}>
          Upload document
        </button>
      </div>
      {isLoading && <p>Loading documents...</p>}
      {error && <ErrorNotice error={error} />}
      {data && (
        <DataTable
          columns={columns}
          rows={data.items}
          getRowKey={(d) => d.document_id}
          onRowClick={(d) => setOpen(d.document_id)}
          emptyMessage="No documents."
        />
      )}

      {uploading && <DocumentUploadDialog team={team} onClose={() => setUploading(false)} />}
      {open && <DocumentDialog team={team} documentId={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

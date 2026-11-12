import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  forgetDocument,
  formatBytes,
  getDocument,
  previewForgetDocument,
  promoteDocument,
  retractDocument,
  type DocumentRevision,
} from "../api/documents";
import { Badge } from "./Badge";
import { type Column, DataTable } from "./DataTable";
import { DocumentUploadDialog } from "./DocumentUploadDialog";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import { Timestamp } from "./Timestamp";
import { statusTone } from "./documentStatus";
import styles from "./ui.module.css";

interface DocumentDialogProps {
  team: string;
  documentId: string;
  onClose: () => void;
}

type Action = "none" | "revision" | "retract" | "forget";

export function DocumentDialog({ team, documentId, onClose }: DocumentDialogProps) {
  const queryClient = useQueryClient();
  const [action, setAction] = useState<Action>("none");
  const [reason, setReason] = useState("");
  const [typedTitle, setTypedTitle] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "teams", team, "documents", documentId],
    queryFn: () => getDocument(team, documentId),
    refetchInterval: (q) => (q.state.data?.revisions.some((r) => r.status === "ingesting") ? 2000 : false),
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "documents"] });

  const retract = useMutation({
    mutationFn: () => retractDocument(team, documentId, reason),
    onSuccess: () => {
      setAction("none");
      refresh();
    },
  });
  const promote = useMutation({ mutationFn: () => promoteDocument(team, documentId), onSuccess: refresh });
  const preview = useMutation({ mutationFn: () => previewForgetDocument(team, documentId) });
  const forget = useMutation({
    mutationFn: (token: string) => forgetDocument(team, documentId, token),
    onSuccess: () => {
      refresh();
      onClose();
    },
  });

  const columns: Column<DocumentRevision>[] = [
    { key: "revision_number", header: "#", render: (r) => r.revision_number },
    { key: "label", header: "Label", render: (r) => r.label ?? "-" },
    { key: "filename", header: "File", render: (r) => r.filename ?? "-" },
    { key: "byte_size", header: "Size", render: (r) => formatBytes(r.byte_size) },
    { key: "page_count", header: "Pages", render: (r) => r.page_count ?? "-" },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <Badge tone={statusTone(r.status)} title={r.error ?? undefined}>
          {r.status}
        </Badge>
      ),
    },
    { key: "uploaded_at", header: "Uploaded", render: (r) => <Timestamp value={r.uploaded_at} /> },
  ];

  const failed = data?.revisions.find((r) => r.status === "failed" && r.error);

  return (
    <Modal title={data?.title ?? "Document"} onClose={onClose}>
      {isLoading && <p>Loading document...</p>}
      {error && <ErrorNotice error={error} />}
      {data && (
        <>
          <p>
            <Badge tone={statusTone(data.status)}>{data.status}</Badge> {data.tags.length > 0 && `tags: ${data.tags.join(", ")}`}
          </p>
          {failed && (
            <p className={`${styles.badge} ${styles.bad}`}>
              Revision {failed.revision_number} failed: {failed.error}
            </p>
          )}
          <DataTable columns={columns} rows={data.revisions} getRowKey={(r) => r.revision_id} emptyMessage="No revisions." />

          {data.status !== "retracted" && action === "none" && (
            <div className={styles.formActions}>
              <button type="button" className={styles.buttonSecondary} onClick={() => setAction("revision")}>
                Upload revision
              </button>
              <button
                type="button"
                className={styles.buttonSecondary}
                disabled={data.status !== "ready" || promote.isPending || promote.isSuccess}
                onClick={() => promote.mutate()}
              >
                {promote.isSuccess ? "Promoted" : "Promote to universe"}
              </button>
              <button type="button" className={styles.buttonSecondary} onClick={() => setAction("retract")}>
                Retract
              </button>
              <button
                type="button"
                className={styles.buttonDanger}
                onClick={() => {
                  setAction("forget");
                  setTypedTitle("");
                  preview.mutate();
                }}
              >
                Forget
              </button>
            </div>
          )}
          {data.status === "retracted" && action === "none" && (
            <div className={styles.formActions}>
              <button
                type="button"
                className={styles.buttonDanger}
                onClick={() => {
                  setAction("forget");
                  setTypedTitle("");
                  preview.mutate();
                }}
              >
                Forget
              </button>
            </div>
          )}
          {promote.isError && <ErrorNotice error={promote.error} />}

          {action === "retract" && (
            <div className={styles.section}>
              <h3>Retract</h3>
              <p>Removes the document from search. Its revisions and files are kept.</p>
              <div className={styles.formField}>
                <label htmlFor="retract-reason">Reason (optional)</label>
                <input id="retract-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
              </div>
              {retract.isError && <ErrorNotice error={retract.error} />}
              <div className={styles.formActions}>
                <button type="button" className={styles.buttonSecondary} onClick={() => setAction("none")}>
                  Cancel
                </button>
                <button
                  type="button"
                  className={styles.buttonDanger}
                  disabled={retract.isPending}
                  onClick={() => retract.mutate()}
                >
                  Retract
                </button>
              </div>
            </div>
          )}

          {action === "forget" && (
            <div className={styles.section}>
              <h3>Forget</h3>
              <p>Irreversible: deletes the document, every revision, its log rows and its files.</p>
              {preview.isPending && <p>Loading preview...</p>}
              {preview.isError && <ErrorNotice error={preview.error} />}
              {preview.data && (
                <>
                  <p>
                    {preview.data.revisions} revision(s), {preview.data.figures} figure(s),{" "}
                    {formatBytes(preview.data.bytes)}.
                    {preview.data.promoted_to && " The universe copy made by promotion is kept."}
                  </p>
                  <div className={styles.formField}>
                    <label htmlFor="forget-title">Type the title to enable Forget</label>
                    <input id="forget-title" value={typedTitle} onChange={(e) => setTypedTitle(e.target.value)} />
                  </div>
                  {forget.isError && <ErrorNotice error={forget.error} />}
                  <div className={styles.formActions}>
                    <button type="button" className={styles.buttonSecondary} onClick={() => setAction("none")}>
                      Cancel
                    </button>
                    <button
                      type="button"
                      className={styles.buttonDanger}
                      disabled={typedTitle !== data.title || forget.isPending}
                      onClick={() => preview.data && forget.mutate(preview.data.preview_token)}
                    >
                      {forget.isPending ? "Forgetting..." : "Forget"}
                    </button>
                  </div>
                </>
              )}
            </div>
          )}
        </>
      )}

      {action === "revision" && data && (
        <DocumentUploadDialog
          team={team}
          documentId={documentId}
          documentTitle={data.title}
          onClose={() => setAction("none")}
        />
      )}
    </Modal>
  );
}

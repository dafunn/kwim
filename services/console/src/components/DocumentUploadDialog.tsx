import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { documentContentType, formatBytes, uploadDocument, uploadRevision } from "../api/documents";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

interface DocumentUploadDialogProps {
  team: string;
  /** Set to upload a new revision of this document instead of a new document. */
  documentId?: string;
  documentTitle?: string;
  onClose: () => void;
}

export function DocumentUploadDialog({ team, documentId, documentTitle, onClose }: DocumentUploadDialogProps) {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [tags, setTags] = useState("");
  const [label, setLabel] = useState("");

  const upload = useMutation({
    mutationFn: () => {
      if (file === null) throw new Error("no file");
      return documentId
        ? uploadRevision(team, documentId, file, { label })
        : uploadDocument(team, file, { title, tags, label });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "documents"] });
      onClose();
    },
  });

  const supported = file === null || documentContentType(file) !== null;

  return (
    <Modal title={documentId ? `New revision of ${documentTitle ?? "document"}` : "Upload document"} onClose={onClose}>
      <div className={styles.formField}>
        <label htmlFor="document-file">File (.pdf or .md)</label>
        <input
          id="document-file"
          type="file"
          accept=".pdf,.md,.markdown,application/pdf,text/markdown"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
        {file && (
          <span>
            {file.name} ({formatBytes(file.size)})
          </span>
        )}
        {!supported && <p className={`${styles.badge} ${styles.bad}`}>Only .pdf and .md files can be uploaded.</p>}
      </div>
      {!documentId && (
        <>
          <div className={styles.formField}>
            <label htmlFor="document-title">Title (defaults to the file name)</label>
            <input id="document-title" value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div className={styles.formField}>
            <label htmlFor="document-tags">Tags (comma separated)</label>
            <input id="document-tags" value={tags} onChange={(e) => setTags(e.target.value)} />
          </div>
        </>
      )}
      <div className={styles.formField}>
        <label htmlFor="document-label">Revision label (optional)</label>
        <input id="document-label" value={label} onChange={(e) => setLabel(e.target.value)} />
      </div>
      {upload.isError && <ErrorNotice error={upload.error} />}
      <div className={styles.formActions}>
        <button type="button" className={styles.buttonSecondary} onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className={styles.buttonPrimary}
          disabled={file === null || !supported || upload.isPending}
          onClick={() => upload.mutate()}
        >
          {upload.isPending ? "Uploading..." : "Upload"}
        </button>
      </div>
    </Modal>
  );
}

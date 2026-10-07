import { type FormEvent, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { amendSemantic } from "../api/semantic";
import type { SemanticItem } from "../api/semantic";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

interface AmendSemanticDialogProps {
  team: string;
  item: SemanticItem;
  onClose: () => void;
  onSuccess: () => void;
}

/** Semantic items have amend only, having no version chain. */
export function AmendSemanticDialog({ team, item, onClose, onSuccess }: AmendSemanticDialogProps) {
  const [content, setContent] = useState(item.content);
  const [metadataJson, setMetadataJson] = useState(JSON.stringify(item.metadata, null, 2));
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: (metadata: Record<string, unknown> | undefined) =>
      amendSemantic(team, item.id, {
        content: content !== item.content ? content : undefined,
        metadata,
        reason,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "semantic"] });
      onSuccess();
    },
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    let metadata: Record<string, unknown> | undefined;
    const trimmed = metadataJson.trim();
    const original = JSON.stringify(item.metadata, null, 2);
    if (trimmed && trimmed !== original) {
      try {
        metadata = JSON.parse(trimmed);
      } catch {
        setJsonError("metadata must be valid JSON");
        return;
      }
    }
    setJsonError(null);
    mutation.mutate(metadata);
  }

  return (
    <Modal title="Amend semantic item" onClose={onClose}>
      <p>This replaces the current content in place. The prior content is kept only in the commit log.</p>
      <form onSubmit={handleSubmit}>
        <div className={styles.formField}>
          <label htmlFor="amend-semantic-content">Content</label>
          <textarea id="amend-semantic-content" value={content} onChange={(e) => setContent(e.target.value)} />
        </div>
        <div className={styles.formField}>
          <label htmlFor="amend-semantic-metadata">Metadata (JSON)</label>
          <textarea
            id="amend-semantic-metadata"
            value={metadataJson}
            onChange={(e) => setMetadataJson(e.target.value)}
          />
          {jsonError && (
            <p className={`${styles.badge} ${styles.bad}`} role="alert">
              {jsonError}
            </p>
          )}
        </div>
        <div className={styles.formField}>
          <label htmlFor="amend-semantic-reason">Reason (required)</label>
          <textarea
            id="amend-semantic-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            required
          />
        </div>
        {mutation.isError && <ErrorNotice error={mutation.error} />}
        <div className={styles.formActions}>
          <button type="button" className={styles.buttonSecondary} onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={styles.buttonPrimary} disabled={!reason.trim() || mutation.isPending}>
            {mutation.isPending ? "Amending..." : "Amend"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

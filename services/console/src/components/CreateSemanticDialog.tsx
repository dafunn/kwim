import { type FormEvent, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createSemantic } from "../api/semantic";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

export function CreateSemanticDialog({ team, onClose }: { team: string; onClose: () => void }) {
  const [id, setId] = useState("");
  const [content, setContent] = useState("");
  const [metadataJson, setMetadataJson] = useState("");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: (metadata: Record<string, unknown> | undefined) =>
      createSemantic(team, { id: id || undefined, content, metadata }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "semantic"] });
      onClose();
    },
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    let metadata: Record<string, unknown> | undefined;
    if (metadataJson.trim()) {
      try {
        metadata = JSON.parse(metadataJson);
      } catch {
        setJsonError("metadata must be valid JSON");
        return;
      }
    }
    setJsonError(null);
    mutation.mutate(metadata);
  }

  return (
    <Modal title="New semantic item" onClose={onClose}>
      <form onSubmit={handleSubmit}>
        <div className={styles.formField}>
          <label htmlFor="create-semantic-id">Id (optional - generated if left blank)</label>
          <input id="create-semantic-id" value={id} onChange={(e) => setId(e.target.value)} />
        </div>
        <div className={styles.formField}>
          <label htmlFor="create-semantic-content">Content</label>
          <textarea id="create-semantic-content" value={content} onChange={(e) => setContent(e.target.value)} required />
        </div>
        <div className={styles.formField}>
          <label htmlFor="create-semantic-metadata">Metadata (JSON, optional)</label>
          <textarea id="create-semantic-metadata" value={metadataJson} onChange={(e) => setMetadataJson(e.target.value)} />
          {jsonError && (
            <p className={`${styles.badge} ${styles.bad}`} role="alert">
              {jsonError}
            </p>
          )}
        </div>
        {mutation.isError && <ErrorNotice error={mutation.error} />}
        <div className={styles.formActions}>
          <button type="button" className={styles.buttonSecondary} onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={styles.buttonPrimary} disabled={!content.trim() || mutation.isPending}>
            {mutation.isPending ? "Creating..." : "Create"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

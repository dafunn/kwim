import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { executeForget, previewForget, type ForgetPreviewResponse } from "../api/objects";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import { Timestamp } from "./Timestamp";
import styles from "./ui.module.css";

interface ForgetDialogProps {
  team: string;
  objectId: string;
  objectType: "fact" | "rule";
  onClose: () => void;
  onSuccess: () => void;
}

/**
 * Preview first, always. A 409 on execute means the set moved under the
 * preview: the token is discarded and a fresh preview is fetched
 * automatically, but execution itself is never retried - the operator must
 * look at the new plan and re-type the new count.
 */
export function ForgetDialog({ team, objectId, objectType, onClose, onSuccess }: ForgetDialogProps) {
  const queryClient = useQueryClient();
  const [typedCount, setTypedCount] = useState("");
  const [stale, setStale] = useState(false);

  const preview = useMutation({
    mutationFn: () => previewForget(team, { object_ids: [objectId] }),
    onSuccess: () => setTypedCount(""),
  });

  useEffect(() => {
    preview.mutate();
    // Only ever fires the initial preview - re-previews are explicit (button or 409 handling).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const execute = useMutation({
    mutationFn: (data: ForgetPreviewResponse) =>
      executeForget(team, { preview_token: data.preview_token, confirm_count: data.count }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team] });
      onSuccess();
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        setStale(true);
        preview.mutate();
      }
    },
  });

  const data = preview.data;
  const countMatches = data !== undefined && typedCount.trim() !== "" && Number(typedCount) === data.count;

  return (
    <Modal title={`Forget ${objectType}`} onClose={onClose}>
      <p>
        Irreversible: hard-deletes this {objectType} and its unshared evidence. Shared episodic events (still
        supporting something else) are kept.
      </p>

      {preview.isPending && <p>Loading preview...</p>}
      {preview.isError && <ErrorNotice error={preview.error} />}

      {stale && (
        <p className={`${styles.badge} ${styles.warn}`} role="alert">
          The set changed since the last preview - showing the updated plan. Review it and confirm again.
        </p>
      )}

      {data && (
        <>
          <div className={styles.section}>
            <h3>Plan ({data.count} object{data.count === 1 ? "" : "s"})</h3>
            {data.plan.map((item) => (
              <div key={item.id} className={styles.formField}>
                <strong>
                  {item.type}: {item.label ?? item.id}
                </strong>
                <span>status: {item.status}</span>
                <span>episodic events to delete: {item.episodics_to_delete.length}</span>
                {item.episodics_shared.length > 0 && (
                  <span>
                    kept (shared with other objects): {item.episodics_shared.map((s) => s.episodic).join(", ")}
                  </span>
                )}
              </div>
            ))}
            {!data.preflight.can_delete && (
              <p className={`${styles.badge} ${styles.bad}`}>
                Postgres role {data.preflight.role ?? "?"} cannot DELETE - forget will fail preflight.
              </p>
            )}
            <p>
              Preview expires <Timestamp value={data.expires_at} />
            </p>
          </div>

          <div className={styles.formField}>
            <label htmlFor="forget-confirm-count">Type {data.count} to enable Forget</label>
            <input
              id="forget-confirm-count"
              value={typedCount}
              onChange={(e) => setTypedCount(e.target.value)}
              inputMode="numeric"
            />
          </div>

          {execute.isError && !(execute.error instanceof ApiError && execute.error.status === 409) && (
            <ErrorNotice error={execute.error} />
          )}

          <div className={styles.formActions}>
            <button type="button" className={styles.buttonSecondary} onClick={onClose}>
              Cancel
            </button>
            <button
              type="button"
              className={styles.buttonSecondary}
              onClick={() => {
                setStale(false);
                preview.mutate();
              }}
            >
              Re-preview
            </button>
            <button
              type="button"
              className={styles.buttonDanger}
              disabled={!countMatches || !data.preflight.can_delete || execute.isPending}
              onClick={() => execute.mutate(data)}
            >
              {execute.isPending ? "Forgetting..." : "Forget"}
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}

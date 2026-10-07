import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { destroyTeamExecute, destroyTeamPreview, type DestroyPreviewResponse } from "../api/teams";
import { ErrorNotice } from "./ErrorNotice";
import { JsonViewer } from "./JsonViewer";
import { Modal } from "./Modal";
import { Timestamp } from "./Timestamp";
import styles from "./ui.module.css";

interface DestroyTeamDialogProps {
  team: string;
  onClose: () => void;
}

/**
 * Two typed confirmations, not one (TeamDestroyRequest: confirm_team and
 * confirm_commit_rows). A 409 on execute means the team changed since the
 * preview - re-preview automatically and make the operator confirm the
 * new numbers, never resend the same confirm_commit_rows.
 */
export function DestroyTeamDialog({ team, onClose }: DestroyTeamDialogProps) {
  const queryClient = useQueryClient();
  const [confirmTeam, setConfirmTeam] = useState("");
  const [confirmRows, setConfirmRows] = useState("");
  const [stale, setStale] = useState(false);

  const preview = useMutation({
    mutationFn: () => destroyTeamPreview(team),
    onSuccess: () => {
      setConfirmTeam("");
      setConfirmRows("");
    },
  });

  useEffect(() => {
    preview.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const execute = useMutation({
    mutationFn: (data: DestroyPreviewResponse) =>
      destroyTeamExecute(team, {
        preview_token: data.preview_token,
        confirm_team: confirmTeam,
        confirm_commit_rows: Number(confirmRows),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams"] });
      onClose();
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        setStale(true);
        preview.mutate();
      }
    },
  });

  const data = preview.data;
  const teamMatches = confirmTeam === team;
  const rowsMatch = data !== undefined && confirmRows.trim() !== "" && Number(confirmRows) === data.counts.commit_rows;

  return (
    <Modal title={`Destroy ${team}`} onClose={onClose}>
      <p className={`${styles.badge} ${styles.bad}`} role="alert">
        Irreversible: drops the schema and both graphs. The team must already be decommissioned.
      </p>

      {preview.isPending && <p>Loading preview...</p>}
      {preview.isError && <ErrorNotice error={preview.error} />}

      {stale && (
        <p className={`${styles.badge} ${styles.warn}`} role="alert">
          The team changed since the last preview - showing the updated counts. Review and confirm again.
        </p>
      )}

      {data && (
        <>
          <div className={styles.section}>
            <h3>What gets dropped</h3>
            <p>
              schema: <code>{data.objects.schema}</code> | graphs: {data.objects.graphs.join(", ")}
            </p>
            <JsonViewer value={data.counts} label="counts" defaultOpen />
            <p>
              Preview expires <Timestamp value={data.expires_at} />
            </p>
          </div>

          <div className={styles.formField}>
            <label htmlFor="destroy-confirm-team">Type the team id ({team}) to confirm</label>
            <input id="destroy-confirm-team" value={confirmTeam} onChange={(e) => setConfirmTeam(e.target.value)} />
          </div>
          <div className={styles.formField}>
            <label htmlFor="destroy-confirm-rows">Type the commit-row count ({data.counts.commit_rows})</label>
            <input
              id="destroy-confirm-rows"
              value={confirmRows}
              onChange={(e) => setConfirmRows(e.target.value)}
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
              disabled={!teamMatches || !rowsMatch || execute.isPending}
              onClick={() => execute.mutate(data)}
            >
              {execute.isPending ? "Destroying..." : "Destroy"}
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}

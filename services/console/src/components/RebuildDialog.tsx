import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { triggerRebuild } from "../api/teams";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

/** A recovery operation, not a routine one: the graph is reconstructed entirely from the commit log - anything not in the log is not restored. */
export function RebuildDialog({ team, onClose }: { team: string; onClose: () => void }) {
  const [skipSemantic, setSkipSemantic] = useState(false);
  const [inPlace, setInPlace] = useState(false);

  const mutation = useMutation({
    mutationFn: () => triggerRebuild(team, { skip_semantic: skipSemantic, in_place: inPlace }),
  });

  if (mutation.isSuccess) {
    return (
      <Modal title="Rebuild started" onClose={onClose}>
        <p>
          Job <code>{mutation.data.job_id}</code> is running. Track it from Console administration.
        </p>
        <div className={styles.formActions}>
          <button type="button" className={styles.buttonPrimary} onClick={onClose}>
            Close
          </button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title={`Rebuild ${team}`} onClose={onClose}>
      <p className={`${styles.badge} ${styles.warn}`} role="alert">
        Recovery operation: the graph is reconstructed entirely by replaying the commit log. Anything not in the log
        is not restored.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        <label className={styles.checklistItem}>
          <input type="checkbox" checked={skipSemantic} onChange={(e) => setSkipSemantic(e.target.checked)} />
          Skip semantic re-embedding
        </label>
        <label className={styles.checklistItem}>
          <input type="checkbox" checked={inPlace} onChange={(e) => setInPlace(e.target.checked)} />
          Rebuild in place (skip the temp-graph swap)
        </label>
        {mutation.isError && <ErrorNotice error={mutation.error} />}
        <div className={styles.formActions}>
          <button type="button" className={styles.buttonSecondary} onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={styles.buttonDanger} disabled={mutation.isPending}>
            {mutation.isPending ? "Starting..." : "Start rebuild"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

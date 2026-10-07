import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { decommissionTeam } from "../api/teams";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

export function DecommissionDialog({ team, onClose }: { team: string; onClose: () => void }) {
  const [reason, setReason] = useState("");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: () => decommissionTeam(team, reason),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams"] });
      onClose();
    },
  });

  return (
    <Modal title={`Decommission ${team}`} onClose={onClose}>
      <p>Revokes all of this team's live keys immediately. The team can be restored later; nothing is deleted.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        <div className={styles.formField}>
          <label htmlFor="decommission-reason">Reason (required)</label>
          <textarea id="decommission-reason" value={reason} onChange={(e) => setReason(e.target.value)} required />
        </div>
        {mutation.isError && <ErrorNotice error={mutation.error} />}
        <div className={styles.formActions}>
          <button type="button" className={styles.buttonSecondary} onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={styles.buttonDanger} disabled={!reason.trim() || mutation.isPending}>
            {mutation.isPending ? "Decommissioning..." : "Decommission"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

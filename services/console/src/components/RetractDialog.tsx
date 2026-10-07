import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { retractObject } from "../api/objects";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

interface RetractDialogProps {
  team: string;
  objectId: string;
  objectType: "fact" | "rule";
  onClose: () => void;
  onSuccess: () => void;
}

export function RetractDialog({ team, objectId, objectType, onClose, onSuccess }: RetractDialogProps) {
  const [reason, setReason] = useState("");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: () => retractObject(team, objectId, { object_type: objectType, reason }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, `${objectType}s`] });
      onSuccess();
    },
  });

  return (
    <Modal title={`Retract ${objectType}`} onClose={onClose}>
      <p>Flips this {objectType} to retracted. It stops being served immediately; this cannot be undone from here.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        <div className={styles.formField}>
          <label htmlFor="retract-reason">Reason (required)</label>
          <textarea id="retract-reason" value={reason} onChange={(e) => setReason(e.target.value)} required />
        </div>
        {mutation.isError && <ErrorNotice error={mutation.error} />}
        <div className={styles.formActions}>
          <button type="button" className={styles.buttonSecondary} onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={styles.buttonDanger} disabled={!reason.trim() || mutation.isPending}>
            {mutation.isPending ? "Retracting..." : "Retract"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { confirmObject } from "../api/objects";
import { ErrorNotice } from "./ErrorNotice";
import styles from "./ui.module.css";

interface ConfirmObjectButtonProps {
  team: string;
  objectId: string;
  objectType: "fact" | "rule";
  onSuccess: () => void;
}

/** Not destructive (no status change, gate.py) - a click-to-arm inline control rather than a full dialog. */
export function ConfirmObjectButton({ team, objectId, objectType, onSuccess }: ConfirmObjectButtonProps) {
  const [armed, setArmed] = useState(false);
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: () => confirmObject(team, objectId, { object_type: objectType }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, `${objectType}s`] });
      onSuccess();
    },
  });

  if (!armed) {
    return (
      <button type="button" className={styles.buttonSecondary} onClick={() => setArmed(true)}>
        Confirm
      </button>
    );
  }

  return (
    <span className={styles.actionRow}>
      <span>Stamp this {objectType} as confirmed?</span>
      <button type="button" className={styles.buttonPrimary} onClick={() => mutation.mutate()} disabled={mutation.isPending}>
        {mutation.isPending ? "Confirming..." : "Yes, confirm"}
      </button>
      <button type="button" className={styles.buttonSecondary} onClick={() => setArmed(false)}>
        Cancel
      </button>
      {mutation.isError && <ErrorNotice error={mutation.error} />}
    </span>
  );
}

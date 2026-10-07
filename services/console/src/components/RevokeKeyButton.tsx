import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { revokeKey } from "../api/keys";
import { ErrorNotice } from "./ErrorNotice";
import styles from "./ui.module.css";

export function RevokeKeyButton({ team, keyPrefix }: { team: string; keyPrefix: string }) {
  const [armed, setArmed] = useState(false);
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: () => revokeKey(team, keyPrefix),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "keys"] }),
  });

  if (!armed) {
    return (
      <button type="button" className={styles.buttonDanger} onClick={() => setArmed(true)}>
        Revoke
      </button>
    );
  }

  return (
    <span className={styles.actionRow}>
      <button type="button" className={styles.buttonDanger} onClick={() => mutation.mutate()} disabled={mutation.isPending}>
        {mutation.isPending ? "Revoking..." : "Confirm revoke"}
      </button>
      <button type="button" className={styles.buttonSecondary} onClick={() => setArmed(false)}>
        Cancel
      </button>
      {mutation.isError && <ErrorNotice error={mutation.error} />}
    </span>
  );
}

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createTeam } from "../api/teams";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

export function CreateTeamDialog({ onClose }: { onClose: () => void }) {
  const [team, setTeam] = useState("");
  const [displayName, setDisplayName] = useState("");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: () => createTeam(team, displayName || undefined),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams"] });
      onClose();
    },
  });

  return (
    <Modal title="Create team" onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        <div className={styles.formField}>
          <label htmlFor="create-team-id">Team identifier</label>
          <input
            id="create-team-id"
            value={team}
            onChange={(e) => setTeam(e.target.value)}
            placeholder="acme"
            required
          />
        </div>
        <div className={styles.formField}>
          <label htmlFor="create-team-display">Display name (optional)</label>
          <input id="create-team-display" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
        </div>
        {mutation.isError && <ErrorNotice error={mutation.error} />}
        <div className={styles.formActions}>
          <button type="button" className={styles.buttonSecondary} onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={styles.buttonPrimary} disabled={!team.trim() || mutation.isPending}>
            {mutation.isPending ? "Creating..." : "Create"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

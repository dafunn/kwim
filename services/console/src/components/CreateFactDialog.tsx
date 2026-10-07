import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createFact } from "../api/facts";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

/** Commits directly, bypassing the propose/review queue - an operator seeding or backfilling data, not the normal agent path. */
export function CreateFactDialog({ team, onClose }: { team: string; onClose: () => void }) {
  const [statement, setStatement] = useState("");
  const [factType, setFactType] = useState("");
  const [about, setAbout] = useState("");
  const [evidence, setEvidence] = useState("");
  const [sourceKind, setSourceKind] = useState<"agent_proposal" | "repo_sync" | "distiller">("agent_proposal");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: () =>
      createFact(team, {
        statement,
        fact_type: factType,
        about: about.split(",").map((s) => s.trim()).filter(Boolean),
        evidence: evidence.split(",").map((s) => s.trim()).filter(Boolean),
        source_kind: sourceKind,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "facts"] });
      onClose();
    },
  });

  return (
    <Modal title="New fact" onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        <div className={styles.formField}>
          <label htmlFor="create-fact-statement">Statement</label>
          <textarea id="create-fact-statement" value={statement} onChange={(e) => setStatement(e.target.value)} required />
        </div>
        <div className={styles.formField}>
          <label htmlFor="create-fact-type">Fact type</label>
          <input id="create-fact-type" value={factType} onChange={(e) => setFactType(e.target.value)} required />
        </div>
        <div className={styles.formField}>
          <label htmlFor="create-fact-about">About (comma-separated)</label>
          <input id="create-fact-about" value={about} onChange={(e) => setAbout(e.target.value)} />
        </div>
        <div className={styles.formField}>
          <label htmlFor="create-fact-evidence">Evidence (episodic event ids, comma-separated)</label>
          <input id="create-fact-evidence" value={evidence} onChange={(e) => setEvidence(e.target.value)} />
        </div>
        <div className={styles.formField}>
          <label htmlFor="create-fact-source">Source kind</label>
          <select id="create-fact-source" value={sourceKind} onChange={(e) => setSourceKind(e.target.value as typeof sourceKind)}>
            <option value="agent_proposal">agent_proposal</option>
            <option value="repo_sync">repo_sync</option>
            <option value="distiller">distiller</option>
          </select>
        </div>
        {mutation.isError && <ErrorNotice error={mutation.error} />}
        <div className={styles.formActions}>
          <button type="button" className={styles.buttonSecondary} onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={styles.buttonPrimary} disabled={!statement.trim() || !factType.trim() || mutation.isPending}>
            {mutation.isPending ? "Creating..." : "Create"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { supersedeFact, type Fact } from "../api/facts";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

interface SupersedeFactDialogProps {
  team: string;
  fact: Fact;
  onClose: () => void;
  onSuccess: () => void;
}

/** Supersede reads as "replace with a new version": the current entry is kept, marked superseded, and stays visible. */
export function SupersedeFactDialog({ team, fact, onClose, onSuccess }: SupersedeFactDialogProps) {
  const [statement, setStatement] = useState(fact.statement);
  const [factType, setFactType] = useState(fact.fact_type);
  const [about, setAbout] = useState(fact.about.join(", "));
  const [evidence, setEvidence] = useState("");
  const [sourceKind, setSourceKind] = useState("");
  const [reason, setReason] = useState("");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: () =>
      supersedeFact(team, fact.id, {
        statement,
        fact_type: factType,
        about: about
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        evidence: evidence
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        source_kind: sourceKind || undefined,
        reason,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "facts"] });
      onSuccess();
    },
  });

  return (
    <Modal title="Supersede fact" onClose={onClose}>
      <p>The current entry is kept and marked superseded. This creates a new fact that replaces it.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        <div className={styles.formField}>
          <label htmlFor="supersede-fact-statement">New statement</label>
          <textarea
            id="supersede-fact-statement"
            value={statement}
            onChange={(e) => setStatement(e.target.value)}
            required
          />
        </div>
        <div className={styles.formField}>
          <label htmlFor="supersede-fact-type">Fact type</label>
          <input id="supersede-fact-type" value={factType} onChange={(e) => setFactType(e.target.value)} required />
        </div>
        <div className={styles.formField}>
          <label htmlFor="supersede-fact-about">About (comma-separated)</label>
          <input id="supersede-fact-about" value={about} onChange={(e) => setAbout(e.target.value)} />
        </div>
        <div className={styles.formField}>
          <label htmlFor="supersede-fact-evidence">Evidence (episodic event ids, comma-separated)</label>
          <input id="supersede-fact-evidence" value={evidence} onChange={(e) => setEvidence(e.target.value)} />
        </div>
        <div className={styles.formField}>
          <label htmlFor="supersede-fact-source">Source kind</label>
          <input id="supersede-fact-source" value={sourceKind} onChange={(e) => setSourceKind(e.target.value)} />
        </div>
        <div className={styles.formField}>
          <label htmlFor="supersede-fact-reason">Reason (required)</label>
          <textarea
            id="supersede-fact-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            required
          />
        </div>
        {mutation.isError && <ErrorNotice error={mutation.error} />}
        <div className={styles.formActions}>
          <button type="button" className={styles.buttonSecondary} onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className={styles.buttonPrimary}
            disabled={!reason.trim() || !statement.trim() || !factType.trim() || mutation.isPending}
          >
            {mutation.isPending ? "Superseding..." : "Supersede"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

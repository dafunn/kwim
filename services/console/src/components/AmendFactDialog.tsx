import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { amendFact, type Fact } from "../api/facts";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

interface AmendFactDialogProps {
  team: string;
  fact: Fact;
  onClose: () => void;
  onSuccess: () => void;
}

/** Amend reads as "correct this entry": the current text is replaced, and the prior text survives only in the commit log's previous_payload. */
export function AmendFactDialog({ team, fact, onClose, onSuccess }: AmendFactDialogProps) {
  const [statement, setStatement] = useState(fact.statement);
  const [factType, setFactType] = useState(fact.fact_type);
  const [about, setAbout] = useState(fact.about.join(", "));
  const [reason, setReason] = useState("");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: () =>
      amendFact(team, fact.id, {
        statement: statement !== fact.statement ? statement : undefined,
        fact_type: factType !== fact.fact_type ? factType : undefined,
        about:
          about !== fact.about.join(", ")
            ? about
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean)
            : undefined,
        reason,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "facts"] });
      onSuccess();
    },
  });

  return (
    <Modal title="Amend fact" onClose={onClose}>
      <p>This replaces the current text in place. The prior text is kept only in the commit log.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        <div className={styles.formField}>
          <label htmlFor="amend-fact-statement">Statement</label>
          <textarea id="amend-fact-statement" value={statement} onChange={(e) => setStatement(e.target.value)} />
        </div>
        <div className={styles.formField}>
          <label htmlFor="amend-fact-type">Fact type</label>
          <input id="amend-fact-type" value={factType} onChange={(e) => setFactType(e.target.value)} />
        </div>
        <div className={styles.formField}>
          <label htmlFor="amend-fact-about">About (comma-separated)</label>
          <input id="amend-fact-about" value={about} onChange={(e) => setAbout(e.target.value)} />
        </div>
        <div className={styles.formField}>
          <label htmlFor="amend-fact-reason">Reason (required)</label>
          <textarea id="amend-fact-reason" value={reason} onChange={(e) => setReason(e.target.value)} required />
        </div>
        {mutation.isError && <ErrorNotice error={mutation.error} />}
        <div className={styles.formActions}>
          <button type="button" className={styles.buttonSecondary} onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={styles.buttonPrimary} disabled={!reason.trim() || mutation.isPending}>
            {mutation.isPending ? "Amending..." : "Amend"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

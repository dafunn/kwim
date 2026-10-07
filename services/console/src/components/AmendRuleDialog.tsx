import { type FormEvent, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { amendRule, type Rule } from "../api/rules";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

interface AmendRuleDialogProps {
  team: string;
  rule: Rule;
  onClose: () => void;
  onSuccess: () => void;
}

export function AmendRuleDialog({ team, rule, onClose, onSuccess }: AmendRuleDialogProps) {
  const [approach, setApproach] = useState(rule.approach ?? "");
  const [actionPattern, setActionPattern] = useState(rule.action_pattern ?? "");
  const [situationJson, setSituationJson] = useState(
    rule.situation ? JSON.stringify(rule.situation, null, 2) : "",
  );
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: (situation: Record<string, unknown> | undefined) =>
      amendRule(team, rule.id, {
        situation,
        approach: approach !== (rule.approach ?? "") ? approach : undefined,
        action_pattern: actionPattern !== (rule.action_pattern ?? "") ? actionPattern : undefined,
        reason,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "rules"] });
      onSuccess();
    },
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    let situation: Record<string, unknown> | undefined;
    if (situationJson.trim()) {
      try {
        situation = JSON.parse(situationJson);
      } catch {
        setJsonError("situation must be valid JSON");
        return;
      }
    }
    setJsonError(null);
    mutation.mutate(situation);
  }

  return (
    <Modal title="Amend rule" onClose={onClose}>
      <p>This replaces the current content in place. The prior content is kept only in the commit log.</p>
      <form onSubmit={handleSubmit}>
        <div className={styles.formField}>
          <label htmlFor="amend-rule-approach">Approach</label>
          <textarea id="amend-rule-approach" value={approach} onChange={(e) => setApproach(e.target.value)} />
        </div>
        <div className={styles.formField}>
          <label htmlFor="amend-rule-action-pattern">Action pattern</label>
          <input
            id="amend-rule-action-pattern"
            value={actionPattern}
            onChange={(e) => setActionPattern(e.target.value)}
          />
        </div>
        <div className={styles.formField}>
          <label htmlFor="amend-rule-situation">Situation (JSON)</label>
          <textarea
            id="amend-rule-situation"
            value={situationJson}
            onChange={(e) => setSituationJson(e.target.value)}
          />
          {jsonError && (
            <p className={`${styles.badge} ${styles.bad}`} role="alert">
              {jsonError}
            </p>
          )}
        </div>
        <div className={styles.formField}>
          <label htmlFor="amend-rule-reason">Reason (required)</label>
          <textarea id="amend-rule-reason" value={reason} onChange={(e) => setReason(e.target.value)} required />
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

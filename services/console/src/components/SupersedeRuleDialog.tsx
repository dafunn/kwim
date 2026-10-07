import { type FormEvent, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { supersedeRule, type Rule } from "../api/rules";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

interface SupersedeRuleDialogProps {
  team: string;
  rule: Rule;
  onClose: () => void;
  onSuccess: () => void;
}

export function SupersedeRuleDialog({ team, rule, onClose, onSuccess }: SupersedeRuleDialogProps) {
  const [approach, setApproach] = useState(rule.approach ?? "");
  const [situationJson, setSituationJson] = useState(rule.situation ? JSON.stringify(rule.situation, null, 2) : "{}");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [evidence, setEvidence] = useState("");
  const [reason, setReason] = useState("");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: (situation: Record<string, unknown>) =>
      supersedeRule(team, rule.id, {
        rule_type: rule.rule_type,
        situation,
        approach,
        evidence: evidence
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        reason,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "rules"] });
      onSuccess();
    },
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    let situation: Record<string, unknown>;
    try {
      situation = situationJson.trim() ? JSON.parse(situationJson) : {};
    } catch {
      setJsonError("situation must be valid JSON");
      return;
    }
    setJsonError(null);
    mutation.mutate(situation);
  }

  return (
    <Modal title="Supersede rule" onClose={onClose}>
      <p>The current rule is kept and marked superseded. This creates a new rule that replaces it.</p>
      <form onSubmit={handleSubmit}>
        <div className={styles.formField}>
          <label htmlFor="supersede-rule-approach">New approach</label>
          <textarea id="supersede-rule-approach" value={approach} onChange={(e) => setApproach(e.target.value)} required />
        </div>
        <div className={styles.formField}>
          <label htmlFor="supersede-rule-situation">Situation (JSON)</label>
          <textarea
            id="supersede-rule-situation"
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
          <label htmlFor="supersede-rule-evidence">Evidence (episodic event ids, comma-separated)</label>
          <input id="supersede-rule-evidence" value={evidence} onChange={(e) => setEvidence(e.target.value)} />
        </div>
        <div className={styles.formField}>
          <label htmlFor="supersede-rule-reason">Reason (required)</label>
          <textarea id="supersede-rule-reason" value={reason} onChange={(e) => setReason(e.target.value)} required />
        </div>
        {mutation.isError && <ErrorNotice error={mutation.error} />}
        <div className={styles.formActions}>
          <button type="button" className={styles.buttonSecondary} onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={styles.buttonPrimary} disabled={!reason.trim() || !approach.trim() || mutation.isPending}>
            {mutation.isPending ? "Superseding..." : "Supersede"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

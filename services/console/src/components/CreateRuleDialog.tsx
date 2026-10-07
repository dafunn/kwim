import { type FormEvent, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createRule } from "../api/rules";
import { ErrorNotice } from "./ErrorNotice";
import { Modal } from "./Modal";
import styles from "./ui.module.css";

/** Advisory commits directly; constraint always routes to review - enforcement policy is too consequential to auto-commit (admin_write.py). */
export function CreateRuleDialog({ team, onClose }: { team: string; onClose: () => void }) {
  const [ruleType, setRuleType] = useState<"advisory" | "constraint">("advisory");
  const [approach, setApproach] = useState("");
  const [situationJson, setSituationJson] = useState("{}");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [evidence, setEvidence] = useState("");
  const [actionPattern, setActionPattern] = useState("");
  const [verdict, setVerdict] = useState<"allow" | "deny" | "escalate">("deny");
  const [authority, setAuthority] = useState("");
  const [severity, setSeverity] = useState("");
  const [checkTier, setCheckTier] = useState<"deterministic" | "classifier">("deterministic");
  const [result, setResult] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: (situation: Record<string, unknown>) =>
      ruleType === "advisory"
        ? createRule(team, {
            rule_type: "advisory",
            situation,
            approach,
            evidence: evidence.split(",").map((s) => s.trim()).filter(Boolean),
          })
        : createRule(team, {
            rule_type: "constraint",
            action_pattern: actionPattern,
            verdict,
            authority,
            severity,
            check_tier: checkTier,
          }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "rules"] });
      queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "proposals"] });
      setResult("proposal_id" in data ? "Sent to review as a pending proposal." : "Committed.");
    },
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (ruleType !== "advisory") {
      mutation.mutate({});
      return;
    }
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

  if (result) {
    return (
      <Modal title="New rule" onClose={onClose}>
        <p>{result}</p>
        <div className={styles.formActions}>
          <button type="button" className={styles.buttonPrimary} onClick={onClose}>
            Close
          </button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="New rule" onClose={onClose}>
      <form onSubmit={handleSubmit}>
        <div className={styles.formField}>
          <label htmlFor="create-rule-type">Rule type</label>
          <select id="create-rule-type" value={ruleType} onChange={(e) => setRuleType(e.target.value as typeof ruleType)}>
            <option value="advisory">advisory</option>
            <option value="constraint">constraint</option>
          </select>
        </div>

        {ruleType === "advisory" ? (
          <>
            <div className={styles.formField}>
              <label htmlFor="create-rule-approach">Approach</label>
              <textarea id="create-rule-approach" value={approach} onChange={(e) => setApproach(e.target.value)} required />
            </div>
            <div className={styles.formField}>
              <label htmlFor="create-rule-situation">Situation (JSON)</label>
              <textarea id="create-rule-situation" value={situationJson} onChange={(e) => setSituationJson(e.target.value)} />
              {jsonError && (
                <p className={`${styles.badge} ${styles.bad}`} role="alert">
                  {jsonError}
                </p>
              )}
            </div>
            <div className={styles.formField}>
              <label htmlFor="create-rule-evidence">Evidence (episodic event ids, comma-separated)</label>
              <input id="create-rule-evidence" value={evidence} onChange={(e) => setEvidence(e.target.value)} />
            </div>
          </>
        ) : (
          <>
            <p className={`${styles.badge} ${styles.warn}`}>Constraint rules always go to human review, not straight to the graph.</p>
            <div className={styles.formField}>
              <label htmlFor="create-rule-pattern">Action pattern</label>
              <input id="create-rule-pattern" value={actionPattern} onChange={(e) => setActionPattern(e.target.value)} required />
            </div>
            <div className={styles.formField}>
              <label htmlFor="create-rule-verdict">Verdict</label>
              <select id="create-rule-verdict" value={verdict} onChange={(e) => setVerdict(e.target.value as typeof verdict)}>
                <option value="allow">allow</option>
                <option value="deny">deny</option>
                <option value="escalate">escalate</option>
              </select>
            </div>
            <div className={styles.formField}>
              <label htmlFor="create-rule-authority">Authority</label>
              <input id="create-rule-authority" value={authority} onChange={(e) => setAuthority(e.target.value)} required />
            </div>
            <div className={styles.formField}>
              <label htmlFor="create-rule-severity">Severity</label>
              <input id="create-rule-severity" value={severity} onChange={(e) => setSeverity(e.target.value)} required />
            </div>
            <div className={styles.formField}>
              <label htmlFor="create-rule-tier">Check tier</label>
              <select id="create-rule-tier" value={checkTier} onChange={(e) => setCheckTier(e.target.value as typeof checkTier)}>
                <option value="deterministic">deterministic</option>
                <option value="classifier">classifier</option>
              </select>
            </div>
          </>
        )}

        {mutation.isError && <ErrorNotice error={mutation.error} />}
        <div className={styles.formActions}>
          <button type="button" className={styles.buttonSecondary} onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={styles.buttonPrimary} disabled={mutation.isPending}>
            {mutation.isPending ? "Creating..." : "Create"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

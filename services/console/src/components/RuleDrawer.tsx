import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getRule } from "../api/rules";
import { AmendRuleDialog } from "./AmendRuleDialog";
import { CommitLogTable } from "./CommitLogTable";
import { ConfirmObjectButton } from "./ConfirmObjectButton";
import { ErrorNotice } from "./ErrorNotice";
import { ForgetDialog } from "./ForgetDialog";
import { JsonViewer } from "./JsonViewer";
import { DrawerSection, ObjectDrawer } from "./ObjectDrawer";
import { RetractDialog } from "./RetractDialog";
import { SupersedeRuleDialog } from "./SupersedeRuleDialog";
import styles from "./ui.module.css";

type OpenDialog = "amend" | "supersede" | "retract" | "forget" | null;

export function RuleDrawer({ team, ruleId, onClose }: { team: string; ruleId: string; onClose: () => void }) {
  const [dialog, setDialog] = useState<OpenDialog>(null);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["admin", "teams", team, "rules", ruleId],
    queryFn: () => getRule(team, ruleId),
  });

  const isApproved = data?.rule.status === "approved";

  return (
    <ObjectDrawer title={data ? data.rule.approach ?? data.rule.action_pattern ?? data.rule.id : "Rule"} onClose={onClose}>
      {isLoading && <p>Loading...</p>}
      {error && <ErrorNotice error={error} />}
      {data && (
        <>
          <DrawerSection title="Rule">
            <p>
              <strong>{data.rule.rule_type}</strong> | {data.rule.status} | scope: {data.rule.scope ?? "-"}
            </p>
            {data.rule.situation && <JsonViewer value={data.rule.situation} label="situation" />}
            {data.rule.verdict && (
              <p>
                verdict: {data.rule.verdict} | authority: {data.rule.authority ?? "-"} | severity:{" "}
                {data.rule.severity ?? "-"}
              </p>
            )}
            <p>evidence: {data.rule.evidence_count}</p>
          </DrawerSection>

          <div className={styles.actionRow}>
            <button type="button" className={styles.buttonSecondary} onClick={() => setDialog("amend")} disabled={!isApproved}>
              Amend
            </button>
            <button
              type="button"
              className={styles.buttonSecondary}
              onClick={() => setDialog("supersede")}
              disabled={!isApproved}
            >
              Supersede
            </button>
            <button
              type="button"
              className={styles.buttonSecondary}
              onClick={() => setDialog("retract")}
              disabled={!isApproved}
            >
              Retract
            </button>
            <ConfirmObjectButton team={team} objectId={ruleId} objectType="rule" onSuccess={refetch} />
            <button type="button" className={styles.buttonDanger} onClick={() => setDialog("forget")}>
              Forget
            </button>
          </div>

          <DrawerSection title="Provenance">
            <p>proposed by: {data.provenance.proposed_by ?? "-"}</p>
            <p>learned from: {data.provenance.learned_from ?? "-"}</p>
            <p>
              promoted from: {data.provenance.promoted_from_id ?? "-"}
              {data.provenance.promoted_from_team ? ` (${data.provenance.promoted_from_team})` : ""}
            </p>
          </DrawerSection>
          <DrawerSection title="Commit log">
            <CommitLogTable rows={data.commit_rows} />
          </DrawerSection>

          {dialog === "amend" && (
            <AmendRuleDialog team={team} rule={data.rule} onClose={() => setDialog(null)} onSuccess={() => { setDialog(null); refetch(); }} />
          )}
          {dialog === "supersede" && (
            <SupersedeRuleDialog
              team={team}
              rule={data.rule}
              onClose={() => setDialog(null)}
              onSuccess={() => { setDialog(null); onClose(); }}
            />
          )}
          {dialog === "retract" && (
            <RetractDialog
              team={team}
              objectId={ruleId}
              objectType="rule"
              onClose={() => setDialog(null)}
              onSuccess={() => { setDialog(null); refetch(); }}
            />
          )}
          {dialog === "forget" && (
            <ForgetDialog
              team={team}
              objectId={ruleId}
              objectType="rule"
              onClose={() => setDialog(null)}
              onSuccess={() => { setDialog(null); onClose(); }}
            />
          )}
        </>
      )}
    </ObjectDrawer>
  );
}

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getFact } from "../api/facts";
import { AmendFactDialog } from "./AmendFactDialog";
import { CommitLogTable } from "./CommitLogTable";
import { ConfirmObjectButton } from "./ConfirmObjectButton";
import { ErrorNotice } from "./ErrorNotice";
import { ForgetDialog } from "./ForgetDialog";
import { Freshness } from "./Freshness";
import { JsonViewer } from "./JsonViewer";
import { DrawerSection, ObjectDrawer } from "./ObjectDrawer";
import { RetractDialog } from "./RetractDialog";
import { SupersedeFactDialog } from "./SupersedeFactDialog";
import { Timestamp } from "./Timestamp";
import styles from "./ui.module.css";

type OpenDialog = "amend" | "supersede" | "retract" | "forget" | null;

export function FactDrawer({ team, factId, onClose }: { team: string; factId: string; onClose: () => void }) {
  const [dialog, setDialog] = useState<OpenDialog>(null);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["admin", "teams", team, "facts", factId],
    queryFn: () => getFact(team, factId),
  });

  const isCurrent = data?.fact.status === "current";

  return (
    <ObjectDrawer title={data ? data.fact.statement : "Fact"} onClose={onClose}>
      {isLoading && <p>Loading...</p>}
      {error && <ErrorNotice error={error} />}
      {data && (
        <>
          <DrawerSection title="Fact">
            <p>
              <strong>{data.fact.fact_type}</strong> | {data.fact.status} |{" "}
              <Freshness band={data.fact.freshness} asOf={data.fact.as_of} /> | created{" "}
              <Timestamp value={data.fact.created_at} />
            </p>
            {data.fact.about.length > 0 && <p>about: {data.fact.about.join(", ")}</p>}
          </DrawerSection>

          <div className={styles.actionRow}>
            <button type="button" className={styles.buttonSecondary} onClick={() => setDialog("amend")} disabled={!isCurrent}>
              Amend
            </button>
            <button
              type="button"
              className={styles.buttonSecondary}
              onClick={() => setDialog("supersede")}
              disabled={!isCurrent}
            >
              Supersede
            </button>
            <button
              type="button"
              className={styles.buttonSecondary}
              onClick={() => setDialog("retract")}
              disabled={!isCurrent}
            >
              Retract
            </button>
            <ConfirmObjectButton team={team} objectId={factId} objectType="fact" onSuccess={refetch} />
            <button type="button" className={styles.buttonDanger} onClick={() => setDialog("forget")}>
              Forget
            </button>
          </div>

          <DrawerSection title="Provenance">
            <p>proposed by: {data.provenance.proposed_by ?? "-"}</p>
            <p>supersedes: {data.provenance.supersedes ?? "-"}</p>
            <p>supported by: {data.provenance.supported_by.join(", ") || "-"}</p>
          </DrawerSection>
          <DrawerSection title="Audit chain">
            {data.audit_chain.length === 0 && <p>No prior versions.</p>}
            {data.audit_chain.map((version, i) => (
              <JsonViewer key={i} value={version} label={`version ${data.audit_chain.length - i}`} />
            ))}
          </DrawerSection>
          <DrawerSection title="Commit log">
            <CommitLogTable rows={data.commit_rows} />
          </DrawerSection>

          {dialog === "amend" && (
            <AmendFactDialog team={team} fact={data.fact} onClose={() => setDialog(null)} onSuccess={() => { setDialog(null); refetch(); }} />
          )}
          {dialog === "supersede" && (
            <SupersedeFactDialog
              team={team}
              fact={data.fact}
              onClose={() => setDialog(null)}
              onSuccess={() => { setDialog(null); onClose(); }}
            />
          )}
          {dialog === "retract" && (
            <RetractDialog
              team={team}
              objectId={factId}
              objectType="fact"
              onClose={() => setDialog(null)}
              onSuccess={() => { setDialog(null); refetch(); }}
            />
          )}
          {dialog === "forget" && (
            <ForgetDialog
              team={team}
              objectId={factId}
              objectType="fact"
              onClose={() => setDialog(null)}
              onSuccess={() => { setDialog(null); onClose(); }}
            />
          )}
        </>
      )}
    </ObjectDrawer>
  );
}

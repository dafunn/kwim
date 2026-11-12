import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getSkill,
  getSkillVersion,
  promoteSkill,
  restoreSkill,
  retireSkill,
  setSkillOwner,
  type SkillVersion,
} from "../api/skills";
import { Badge } from "./Badge";
import { type Column, DataTable } from "./DataTable";
import { ErrorNotice } from "./ErrorNotice";
import { lineDiff } from "./lineDiff";
import { Modal } from "./Modal";
import { SkillEditDialog } from "./SkillEditDialog";
import { Timestamp } from "./Timestamp";
import styles from "./ui.module.css";

interface SkillDialogProps {
  team: string;
  name: string;
  onClose: () => void;
}

/** A version's changes from the one before it. */
function VersionDiff({ team, name, version }: { team: string; name: string; version: number }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "teams", team, "skills", name, "diff", version],
    queryFn: async () => {
      const [before, after] = await Promise.all([
        getSkillVersion(team, name, version - 1),
        getSkillVersion(team, name, version),
      ]);
      return lineDiff(before.text ?? "", after.text ?? "");
    },
  });
  if (isLoading) return <p>Loading changes...</p>;
  if (error) return <ErrorNotice error={error} />;
  return (
    <pre className={styles.skillText} aria-label={`changes in version ${version}`}>
      {data?.map((line, i) => (
        <span
          key={i}
          className={line.kind === "added" ? styles.diffAdded : line.kind === "removed" ? styles.diffRemoved : undefined}
        >
          {line.kind === "added" ? "+ " : line.kind === "removed" ? "- " : "  "}
          {line.text}
          {"\n"}
        </span>
      ))}
    </pre>
  );
}

export function SkillDialog({ team, name, onClose }: SkillDialogProps) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [diffOf, setDiffOf] = useState<number | null>(null);
  const [owner, setOwner] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "teams", team, "skills", name],
    queryFn: () => getSkill(team, name),
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "skills"] });
  const retire = useMutation({ mutationFn: () => retireSkill(team, name), onSuccess: refresh });
  const restore = useMutation({ mutationFn: () => restoreSkill(team, name), onSuccess: refresh });
  const promote = useMutation({ mutationFn: () => promoteSkill(team, name), onSuccess: refresh });
  const saveOwner = useMutation({
    mutationFn: (value: string) => setSkillOwner(team, name, value),
    onSuccess: () => {
      setOwner(null);
      refresh();
    },
  });

  const columns: Column<SkillVersion>[] = [
    { key: "version", header: "Version", render: (v) => v.version },
    { key: "source", header: "Source", render: (v) => v.source.replace("_", " ") },
    { key: "author", header: "Author", render: (v) => v.author ?? "-" },
    { key: "approver", header: "Approver", render: (v) => v.approver ?? "-" },
    { key: "created_at", header: "Created", render: (v) => <Timestamp value={v.created_at} /> },
    {
      key: "diff",
      header: "",
      render: (v) =>
        v.version > 1 ? (
          <button type="button" className={styles.buttonSecondary} onClick={() => setDiffOf(diffOf === v.version ? null : v.version)}>
            {diffOf === v.version ? "Hide changes" : "Changes"}
          </button>
        ) : null,
    },
  ];

  return (
    <Modal title={name} onClose={onClose}>
      {isLoading && <p>Loading skill...</p>}
      {error && <ErrorNotice error={error} />}
      {data && (
        <>
          <p>
            <Badge tone={data.status === "current" ? "good" : "neutral"}>{data.status}</Badge> version {data.version}
            {data.agents.length > 0 && ` - agents: ${data.agents.join(", ")}`}
            {data.tags.length > 0 && ` - tags: ${data.tags.join(", ")}`}
          </p>
          <p>{data.description}</p>
          {data.pending_revision && (
            <p className={`${styles.badge} ${styles.warn}`}>
              A revision proposed by {data.pending_revision.proposed_by ?? "an agent"} is waiting for review (Proposals
              tab).
            </p>
          )}
          <div className={styles.section}>
            <h3>Owner</h3>
            {owner === null ? (
              <p>
                {data.owner ?? "none"}{" "}
                <button type="button" className={styles.buttonSecondary} onClick={() => setOwner(data.owner ?? "")}>
                  Change
                </button>
              </p>
            ) : (
              <div className={styles.actionRow}>
                <input aria-label="owner" value={owner} onChange={(e) => setOwner(e.target.value)} />
                <button
                  type="button"
                  className={styles.buttonPrimary}
                  disabled={!owner || saveOwner.isPending}
                  onClick={() => saveOwner.mutate(owner)}
                >
                  Save
                </button>
                <button type="button" className={styles.buttonSecondary} onClick={() => setOwner(null)}>
                  Cancel
                </button>
              </div>
            )}
            {saveOwner.isError && <ErrorNotice error={saveOwner.error} />}
          </div>
          {data.refs.length > 0 && (
            <div className={styles.section}>
              <h3>Files in git</h3>
              <ul>
                {data.refs.map((r) => (
                  <li key={r}>
                    <code>{r}</code>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div className={styles.section}>
            <h3>SKILL.md</h3>
            <pre className={styles.skillText}>{data.text}</pre>
          </div>
          <div className={styles.section}>
            <h3>Versions</h3>
            <DataTable columns={columns} rows={data.versions} getRowKey={(v) => String(v.version)} emptyMessage="No versions." />
            {diffOf !== null && <VersionDiff team={team} name={name} version={diffOf} />}
          </div>
          <div className={styles.formActions}>
            {data.status === "current" ? (
              <>
                <button type="button" className={styles.buttonSecondary} onClick={() => setEditing(true)}>
                  Revise
                </button>
                {team !== "universe" && (
                  <button
                    type="button"
                    className={styles.buttonSecondary}
                    disabled={promote.isPending || promote.isSuccess}
                    onClick={() => promote.mutate()}
                  >
                    {promote.isSuccess ? "Promoted" : "Promote to universe"}
                  </button>
                )}
                <button type="button" className={styles.buttonDanger} disabled={retire.isPending} onClick={() => retire.mutate()}>
                  Retire
                </button>
              </>
            ) : (
              <button type="button" className={styles.buttonPrimary} disabled={restore.isPending} onClick={() => restore.mutate()}>
                Restore
              </button>
            )}
          </div>
          {promote.isSuccess && promote.data.status === "pending_review" && <p>{promote.data.detail}</p>}
          {promote.isError && <ErrorNotice error={promote.error} />}
          {retire.isError && <ErrorNotice error={retire.error} />}
          {restore.isError && <ErrorNotice error={restore.error} />}
        </>
      )}
      {editing && data && (
        <SkillEditDialog team={team} name={name} initialText={data.text} onClose={() => setEditing(false)} />
      )}
    </Modal>
  );
}

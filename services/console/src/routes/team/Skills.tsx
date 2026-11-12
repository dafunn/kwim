import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { listSkills, type SkillSummary } from "../../api/skills";
import { Badge } from "../../components/Badge";
import { type Column, DataTable } from "../../components/DataTable";
import { ErrorNotice } from "../../components/ErrorNotice";
import { SkillDialog } from "../../components/SkillDialog";
import { SkillEditDialog } from "../../components/SkillEditDialog";
import { Timestamp } from "../../components/Timestamp";
import styles from "../../components/ui.module.css";

export function Skills() {
  const { team = "" } = useParams<{ team: string }>();
  const [creating, setCreating] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "teams", team, "skills"],
    queryFn: () => listSkills(team),
  });

  const unowned = data?.items.filter((s) => !s.owner && s.status === "current") ?? [];

  const columns: Column<SkillSummary>[] = [
    { key: "name", header: "Name", render: (s) => s.name },
    { key: "status", header: "Status", render: (s) => <Badge tone={s.status === "current" ? "good" : "neutral"}>{s.status}</Badge> },
    { key: "version", header: "Version", render: (s) => s.version },
    { key: "agents", header: "Agents", render: (s) => s.agents.join(", ") || "all" },
    { key: "owner", header: "Owner", render: (s) => s.owner ?? <Badge tone="warn">none</Badge> },
    {
      key: "pending",
      header: "Pending revision",
      render: (s) => (s.pending_revision ? <Badge tone="warn">waiting</Badge> : "-"),
    },
    { key: "updated_at", header: "Updated", render: (s) => <Timestamp value={s.updated_at} /> },
  ];

  return (
    <div>
      <div className={styles.actionRow}>
        <button type="button" className={styles.buttonPrimary} onClick={() => setCreating(true)}>
          New skill
        </button>
      </div>
      {unowned.length > 0 && (
        <p className={`${styles.badge} ${styles.warn}`}>
          {unowned.length} skill(s) have no owner: {unowned.map((s) => s.name).join(", ")}
        </p>
      )}
      {isLoading && <p>Loading skills...</p>}
      {error && <ErrorNotice error={error} />}
      {data && (
        <DataTable
          columns={columns}
          rows={data.items}
          getRowKey={(s) => s.id}
          onRowClick={(s) => setOpen(s.name)}
          emptyMessage="No skills."
        />
      )}

      {creating && <SkillEditDialog team={team} onClose={() => setCreating(false)} />}
      {open && <SkillDialog team={team} name={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

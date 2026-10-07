import { type MouseEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { adoptTeam, listTeams, restoreTeam, type TeamSummary } from "../api/teams";
import { Badge } from "../components/Badge";
import { CreateTeamDialog } from "../components/CreateTeamDialog";
import { type Column, DataTable } from "../components/DataTable";
import { DecommissionDialog } from "../components/DecommissionDialog";
import { DestroyTeamDialog } from "../components/DestroyTeamDialog";
import { ErrorNotice } from "../components/ErrorNotice";
import { Timestamp } from "../components/Timestamp";
import styles from "../components/ui.module.css";

function driftBadges(team: TeamSummary) {
  if (team.has_schema && !team.has_console_record) {
    return <Badge tone="warn">schema, not adopted</Badge>;
  }
  if (!team.has_schema && team.has_console_record) {
    return <Badge tone="bad">console record, no schema</Badge>;
  }
  return null;
}

/** Stops a button click inside a clickable row from also triggering the row's own navigation. */
function stop(e: MouseEvent) {
  e.stopPropagation();
}

function AdoptButton({ team }: { team: string }) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => adoptTeam(team),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin", "teams"] }),
  });
  return (
    <button type="button" className={styles.buttonSecondary} onClick={(e) => { stop(e); mutation.mutate(); }} disabled={mutation.isPending}>
      {mutation.isPending ? "Adopting..." : "Adopt"}
    </button>
  );
}

function RestoreButton({ team }: { team: string }) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => restoreTeam(team),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin", "teams"] }),
  });
  return (
    <button type="button" className={styles.buttonSecondary} onClick={(e) => { stop(e); mutation.mutate(); }} disabled={mutation.isPending}>
      {mutation.isPending ? "Restoring..." : "Restore"}
    </button>
  );
}

export function TeamsOverview() {
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const [decommissioning, setDecommissioning] = useState<string | null>(null);
  const [destroying, setDestroying] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "teams", { count: true }],
    queryFn: () => listTeams({ count: true }),
  });

  const columns: Column<TeamSummary>[] = [
    { key: "team", header: "Team", render: (t) => t.display_name || t.team },
    {
      key: "status",
      header: "Status",
      render: (t) => (t.status ? <Badge tone={t.status === "decommissioned" ? "bad" : "good"}>{t.status}</Badge> : "-"),
    },
    { key: "facts", header: "Facts", render: (t) => t.counts?.facts ?? "-" },
    { key: "rules", header: "Rules", render: (t) => t.counts?.rules ?? "-" },
    { key: "semantic", header: "Semantic", render: (t) => t.counts?.semantic ?? "-" },
    { key: "episodic", header: "Episodic", render: (t) => t.counts?.episodic ?? "-" },
    { key: "pending", header: "Pending", render: (t) => t.counts?.pending ?? "-" },
    { key: "created_at", header: "Created", render: (t) => <Timestamp value={t.created_at} /> },
    { key: "drift", header: "", render: driftBadges },
    {
      key: "actions",
      header: "",
      render: (t) => (
        <span className={styles.actionRow} onClick={stop}>
          {t.has_schema && !t.has_console_record && <AdoptButton team={t.team} />}
          {t.has_console_record && t.status !== "decommissioned" && (
            <button type="button" className={styles.buttonSecondary} onClick={() => setDecommissioning(t.team)}>
              Decommission
            </button>
          )}
          {t.status === "decommissioned" && <RestoreButton team={t.team} />}
          {t.status === "decommissioned" && (
            <button type="button" className={styles.buttonDanger} onClick={() => setDestroying(t.team)}>
              Destroy
            </button>
          )}
        </span>
      ),
    },
  ];

  return (
    <div>
      <div className={styles.actionRow}>
        <h1>Teams</h1>
        <button type="button" className={styles.buttonPrimary} onClick={() => setCreating(true)}>
          Create team
        </button>
      </div>
      {isLoading && <p>Loading teams...</p>}
      {error && <ErrorNotice error={error} />}
      {data && (
        <DataTable
          columns={columns}
          rows={data.items}
          getRowKey={(t) => t.team}
          onRowClick={(t) => navigate(`/teams/${t.team}/knowledge`)}
          emptyMessage="No teams yet."
        />
      )}

      {creating && <CreateTeamDialog onClose={() => setCreating(false)} />}
      {decommissioning && <DecommissionDialog team={decommissioning} onClose={() => setDecommissioning(null)} />}
      {destroying && <DestroyTeamDialog team={destroying} onClose={() => setDestroying(null)} />}
    </div>
  );
}

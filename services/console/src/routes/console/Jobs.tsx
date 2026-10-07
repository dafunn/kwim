import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getJob, listJobs, type Job } from "../../api/jobs";
import { Badge } from "../../components/Badge";
import { type Column, DataTable } from "../../components/DataTable";
import { ErrorNotice } from "../../components/ErrorNotice";
import { JsonViewer } from "../../components/JsonViewer";
import { DrawerSection, ObjectDrawer } from "../../components/ObjectDrawer";
import { Timestamp } from "../../components/Timestamp";
import styles from "../../components/ui.module.css";

const STATUS_TONE: Record<string, "good" | "bad" | "warn"> = {
  running: "warn",
  succeeded: "good",
  failed: "bad",
};

function JobDrawer({ jobId, onClose }: { jobId: string; onClose: () => void }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "jobs", jobId],
    queryFn: () => getJob(jobId),
    refetchInterval: (query) => (query.state.data?.status === "running" ? 2000 : false),
  });

  return (
    <ObjectDrawer title={`Job ${jobId}`} onClose={onClose}>
      {isLoading && <p>Loading...</p>}
      {error && <ErrorNotice error={error} />}
      {data && (
        <DrawerSection title={data.kind}>
          <p>
            team: {data.team} | <Badge tone={STATUS_TONE[data.status] ?? "neutral"}>{data.status}</Badge>
          </p>
          <p>
            started <Timestamp value={data.started_at} /> | finished <Timestamp value={data.finished_at} />
          </p>
          <JsonViewer value={data.detail} label="detail" defaultOpen />
        </DrawerSection>
      )}
    </ObjectDrawer>
  );
}

export function Jobs() {
  const [team, setTeam] = useState("");
  const [status, setStatus] = useState("");
  const [openJobId, setOpenJobId] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "jobs", { team, status }],
    queryFn: () => listJobs({ team: team || undefined, status: status || undefined }),
    refetchInterval: (query) => (query.state.data?.items.some((j) => j.status === "running") ? 3000 : false),
  });

  const columns: Column<Job>[] = [
    { key: "kind", header: "Kind", render: (j) => j.kind },
    { key: "team", header: "Team", render: (j) => j.team },
    { key: "status", header: "Status", render: (j) => <Badge tone={STATUS_TONE[j.status] ?? "neutral"}>{j.status}</Badge> },
    { key: "started_at", header: "Started", render: (j) => <Timestamp value={j.started_at} /> },
    { key: "finished_at", header: "Finished", render: (j) => <Timestamp value={j.finished_at} /> },
  ];

  return (
    <div>
      <p>Rebuild is triggered from a team's detail screen; this is the status view for every job, of any kind.</p>
      <div className={styles.filterBar}>
        <div className={styles.filterField}>
          <label htmlFor="jobs-team">Team</label>
          <input id="jobs-team" value={team} onChange={(e) => setTeam(e.target.value)} placeholder="any" />
        </div>
        <div className={styles.filterField}>
          <label htmlFor="jobs-status">Status</label>
          <select id="jobs-status" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">any</option>
            <option value="running">running</option>
            <option value="succeeded">succeeded</option>
            <option value="failed">failed</option>
          </select>
        </div>
      </div>
      {isLoading && <p>Loading jobs...</p>}
      {error && <ErrorNotice error={error} />}
      {data && (
        <DataTable
          columns={columns}
          rows={data.items}
          getRowKey={(j) => j.job_id}
          onRowClick={(j) => setOpenJobId(j.job_id)}
          emptyMessage="No jobs."
        />
      )}

      {openJobId && <JobDrawer jobId={openJobId} onClose={() => setOpenJobId(null)} />}
    </div>
  );
}

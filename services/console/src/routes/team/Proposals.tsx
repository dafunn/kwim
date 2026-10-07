import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { approveProposal, rejectProposal, type Proposal, type ProposalFilters } from "../../api/proposals";
import { listProposals } from "../../api/proposals";
import { useCursorList } from "../../api/useCursorList";
import { Badge } from "../../components/Badge";
import { type Column, DataTable } from "../../components/DataTable";
import { ErrorNotice } from "../../components/ErrorNotice";
import { JsonViewer } from "../../components/JsonViewer";
import { LoadMore } from "../../components/LoadMore";
import { Timestamp } from "../../components/Timestamp";
import styles from "../../components/ui.module.css";

const RESOLUTION_TONE: Record<string, "good" | "bad" | "neutral"> = {
  approved: "good",
  rejected: "bad",
};

function ProposalActions({ team, proposal }: { team: string; proposal: Proposal }) {
  const queryClient = useQueryClient();
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["admin", "teams", team, "proposals"] });

  const approve = useMutation({
    mutationFn: () => approveProposal(team, proposal.proposal_id),
    onSuccess: invalidate,
  });
  const reject = useMutation({
    mutationFn: () => rejectProposal(team, proposal.proposal_id, reason || undefined),
    onSuccess: invalidate,
  });

  if (rejecting) {
    return (
      <div className={styles.formField}>
        <input
          placeholder="reject reason (optional)"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        <div className={styles.actionRow}>
          <button type="button" className={styles.buttonDanger} onClick={() => reject.mutate()} disabled={reject.isPending}>
            {reject.isPending ? "Rejecting..." : "Confirm reject"}
          </button>
          <button type="button" className={styles.buttonSecondary} onClick={() => setRejecting(false)}>
            Cancel
          </button>
        </div>
        {reject.isError && <ErrorNotice error={reject.error} />}
      </div>
    );
  }

  return (
    <div className={styles.actionRow}>
      <button type="button" className={styles.buttonPrimary} onClick={() => approve.mutate()} disabled={approve.isPending}>
        {approve.isPending ? "Approving..." : "Approve"}
      </button>
      <button type="button" className={styles.buttonDanger} onClick={() => setRejecting(true)}>
        Reject
      </button>
      {approve.isError && <ErrorNotice error={approve.error} />}
    </div>
  );
}

/** A resolved filter exposes the rejection audit trail, including reject_reason, which no other surface shows. */
export function Proposals() {
  const { team = "" } = useParams<{ team: string }>();
  const [filters, setFilters] = useState<ProposalFilters>({ resolved: "false" });
  const [draft, setDraft] = useState<ProposalFilters>({ resolved: "false" });

  const { items, isLoading, error, hasNextPage, isFetchingNextPage, fetchNextPage } = useCursorList<Proposal>(
    ["admin", "teams", team, "proposals", filters],
    (cursor) => listProposals(team, filters, cursor),
  );

  const showResolution = filters.resolved !== "false";

  const columns: Column<Proposal>[] = [
    { key: "summary", header: "Summary", render: (p) => p.summary },
    { key: "object_type", header: "Object", render: (p) => p.object_type },
    { key: "proposed_by", header: "Proposed by", render: (p) => p.proposed_by ?? "-" },
    { key: "created_at", header: "Created", render: (p) => <Timestamp value={p.created_at} /> },
    { key: "body", header: "Body", render: (p) => <JsonViewer value={p.body} label="body" /> },
    ...(showResolution
      ? [
          {
            key: "resolution",
            header: "Resolution",
            render: (p: Proposal) =>
              p.resolution ? <Badge tone={RESOLUTION_TONE[p.resolution] ?? "neutral"}>{p.resolution}</Badge> : "-",
          },
          { key: "resolved_by", header: "Resolved by", render: (p: Proposal) => p.resolved_by ?? "-" },
          { key: "resolved_at", header: "Resolved at", render: (p: Proposal) => <Timestamp value={p.resolved_at} /> },
          { key: "reject_reason", header: "Reject reason", render: (p: Proposal) => p.reject_reason ?? "-" },
        ]
      : [{ key: "actions", header: "", render: (p: Proposal) => <ProposalActions team={team} proposal={p} /> }]),
  ];

  return (
    <div>
      <form
        className={styles.filterBar}
        onSubmit={(e) => {
          e.preventDefault();
          setFilters(draft);
        }}
      >
        <div className={styles.filterField}>
          <label htmlFor="prop-resolved">Queue</label>
          <select
            id="prop-resolved"
            value={draft.resolved}
            onChange={(e) => setDraft({ ...draft, resolved: e.target.value as ProposalFilters["resolved"] })}
          >
            <option value="false">pending</option>
            <option value="true">resolved</option>
            <option value="any">any</option>
          </select>
        </div>
        <div className={styles.filterField}>
          <label htmlFor="prop-object-type">Object type</label>
          <select
            id="prop-object-type"
            value={draft.object_type ?? ""}
            onChange={(e) => setDraft({ ...draft, object_type: e.target.value || undefined })}
          >
            <option value="">any</option>
            <option value="fact">fact</option>
            <option value="rule">rule</option>
          </select>
        </div>
        <button type="submit">Apply</button>
      </form>

      {isLoading && <p>Loading proposals...</p>}
      {error && <ErrorNotice error={error} />}
      <DataTable columns={columns} rows={items} getRowKey={(p) => p.proposal_id} emptyMessage="No proposals." />
      <LoadMore hasNextPage={hasNextPage} isFetchingNextPage={isFetchingNextPage} onClick={() => fetchNextPage()} />
    </div>
  );
}

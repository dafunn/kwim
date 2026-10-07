import { useState } from "react";
import { useParams } from "react-router-dom";
import type { Rule, RuleFilters } from "../../api/rules";
import { listRules } from "../../api/rules";
import { useCursorList } from "../../api/useCursorList";
import { Badge } from "../../components/Badge";
import { CreateRuleDialog } from "../../components/CreateRuleDialog";
import { type Column, DataTable } from "../../components/DataTable";
import { ErrorNotice } from "../../components/ErrorNotice";
import { LoadMore } from "../../components/LoadMore";
import { RuleDrawer } from "../../components/RuleDrawer";
import styles from "../../components/ui.module.css";

const STATUS_OPTIONS = ["any", "pending", "approved", "deprecated", "retracted"];

const STATUS_TONE: Record<string, "good" | "bad" | "warn" | "neutral"> = {
  approved: "good",
  pending: "warn",
  deprecated: "warn",
  retracted: "bad",
};

export function Wisdom() {
  const { team = "" } = useParams<{ team: string }>();
  const [filters, setFilters] = useState<RuleFilters>({ status: "any" });
  const [draft, setDraft] = useState<RuleFilters>({ status: "any" });
  const [openRuleId, setOpenRuleId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const { items, isLoading, error, hasNextPage, isFetchingNextPage, fetchNextPage } = useCursorList<Rule>(
    ["admin", "teams", team, "rules", filters],
    (cursor) => listRules(team, filters, cursor),
  );

  const columns: Column<Rule>[] = [
    { key: "approach", header: "Approach / pattern", render: (r) => r.approach ?? r.action_pattern ?? "-" },
    { key: "rule_type", header: "Type", render: (r) => r.rule_type },
    { key: "status", header: "Status", render: (r) => <Badge tone={STATUS_TONE[r.status] ?? "neutral"}>{r.status}</Badge> },
    { key: "scope", header: "Scope", render: (r) => r.scope ?? "-" },
    { key: "evidence_count", header: "Evidence", render: (r) => r.evidence_count },
  ];

  return (
    <div>
      <div className={styles.actionRow}>
        <button type="button" className={styles.buttonPrimary} onClick={() => setCreating(true)}>
          New rule
        </button>
      </div>
      <form
        className={styles.filterBar}
        onSubmit={(e) => {
          e.preventDefault();
          setFilters(draft);
        }}
      >
        <div className={styles.filterField}>
          <label htmlFor="rule-status">Status</label>
          <select id="rule-status" value={draft.status} onChange={(e) => setDraft({ ...draft, status: e.target.value })}>
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <div className={styles.filterField}>
          <label htmlFor="rule-type">Rule type</label>
          <input
            id="rule-type"
            value={draft.rule_type ?? ""}
            onChange={(e) => setDraft({ ...draft, rule_type: e.target.value || undefined })}
          />
        </div>
        <div className={styles.filterField}>
          <label htmlFor="rule-scope">Scope</label>
          <input
            id="rule-scope"
            value={draft.scope ?? ""}
            onChange={(e) => setDraft({ ...draft, scope: e.target.value || undefined })}
          />
        </div>
        <button type="submit">Apply</button>
      </form>

      {isLoading && <p>Loading rules...</p>}
      {error && <ErrorNotice error={error} />}
      <DataTable
        columns={columns}
        rows={items}
        getRowKey={(r) => r.id}
        onRowClick={(r) => setOpenRuleId(r.id)}
        emptyMessage="No rules match these filters."
      />
      <LoadMore hasNextPage={hasNextPage} isFetchingNextPage={isFetchingNextPage} onClick={() => fetchNextPage()} />

      {openRuleId && <RuleDrawer team={team} ruleId={openRuleId} onClose={() => setOpenRuleId(null)} />}
      {creating && <CreateRuleDialog team={team} onClose={() => setCreating(false)} />}
    </div>
  );
}

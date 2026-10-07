import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { listAllKeys, type ApiKey } from "../../api/keys";
import { Badge } from "../../components/Badge";
import { type Column, DataTable } from "../../components/DataTable";
import { ErrorNotice } from "../../components/ErrorNotice";
import { RevokeKeyButton } from "../../components/RevokeKeyButton";
import { Timestamp } from "../../components/Timestamp";
import styles from "../../components/ui.module.css";

export function AllKeys() {
  const [team, setTeam] = useState("");
  const [includeRevoked, setIncludeRevoked] = useState(false);

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "keys", { team, includeRevoked }],
    queryFn: () => listAllKeys({ team: team || undefined, includeRevoked }),
  });

  const columns: Column<ApiKey>[] = [
    { key: "team", header: "Team", render: (k) => k.team },
    { key: "label", header: "Label", render: (k) => k.label },
    { key: "key_prefix", header: "Prefix", render: (k) => <code>{k.key_prefix}</code> },
    { key: "capabilities", header: "Capabilities", render: (k) => k.capabilities.join(", ") },
    { key: "last_used_at", header: "Last used", render: (k) => <Timestamp value={k.last_used_at} /> },
    { key: "expires_at", header: "Expires", render: (k) => <Timestamp value={k.expires_at} /> },
    {
      key: "revoked_at",
      header: "Status",
      render: (k) => (k.revoked_at ? <Badge tone="bad">revoked</Badge> : <Badge tone="good">live</Badge>),
    },
    {
      key: "actions",
      header: "",
      render: (k) => (k.revoked_at ? null : <RevokeKeyButton team={k.team} keyPrefix={k.key_prefix} />),
    },
  ];

  return (
    <div>
      <div className={styles.filterBar}>
        <div className={styles.filterField}>
          <label htmlFor="allkeys-team">Team</label>
          <input id="allkeys-team" value={team} onChange={(e) => setTeam(e.target.value)} placeholder="any" />
        </div>
        <label className={styles.filterField}>
          <input type="checkbox" checked={includeRevoked} onChange={(e) => setIncludeRevoked(e.target.checked)} />
          Include revoked
        </label>
      </div>
      {isLoading && <p>Loading keys...</p>}
      {error && <ErrorNotice error={error} />}
      {data && <DataTable columns={columns} rows={data.items} getRowKey={(k) => k.id} emptyMessage="No keys." />}
    </div>
  );
}

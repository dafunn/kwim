import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { listTeamKeys, type ApiKey } from "../../api/keys";
import { Badge } from "../../components/Badge";
import { type Column, DataTable } from "../../components/DataTable";
import { ErrorNotice } from "../../components/ErrorNotice";
import { KeyMintDialog } from "../../components/KeyMintDialog";
import { RevokeKeyButton } from "../../components/RevokeKeyButton";
import { Timestamp } from "../../components/Timestamp";
import styles from "../../components/ui.module.css";

export function Keys() {
  const { team = "" } = useParams<{ team: string }>();
  const [includeRevoked, setIncludeRevoked] = useState(false);
  const [minting, setMinting] = useState(false);

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "teams", team, "keys", includeRevoked],
    queryFn: () => listTeamKeys(team, includeRevoked),
  });

  const columns: Column<ApiKey>[] = [
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
      render: (k) => (k.revoked_at ? null : <RevokeKeyButton team={team} keyPrefix={k.key_prefix} />),
    },
  ];

  return (
    <div>
      <div className={styles.actionRow}>
        <button type="button" className={styles.buttonPrimary} onClick={() => setMinting(true)}>
          Mint key
        </button>
        <label className={styles.filterField}>
          <input type="checkbox" checked={includeRevoked} onChange={(e) => setIncludeRevoked(e.target.checked)} />
          Include revoked
        </label>
      </div>
      {isLoading && <p>Loading keys...</p>}
      {error && <ErrorNotice error={error} />}
      {data && <DataTable columns={columns} rows={data.items} getRowKey={(k) => k.id} emptyMessage="No keys." />}

      {minting && <KeyMintDialog team={team} onClose={() => setMinting(false)} />}
    </div>
  );
}

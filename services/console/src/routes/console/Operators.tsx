import { useQuery } from "@tanstack/react-query";
import { listOperators, type Operator } from "../../api/operators";
import { Badge } from "../../components/Badge";
import { type Column, DataTable } from "../../components/DataTable";
import { ErrorNotice } from "../../components/ErrorNotice";
import { Timestamp } from "../../components/Timestamp";

export function Operators() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "operators"],
    queryFn: listOperators,
  });

  const columns: Column<Operator>[] = [
    { key: "username", header: "Username", render: (o) => o.username },
    { key: "display_name", header: "Display name", render: (o) => o.display_name ?? "-" },
    {
      key: "is_active",
      header: "Status",
      render: (o) => (o.is_active ? <Badge tone="good">active</Badge> : <Badge tone="bad">inactive</Badge>),
    },
    { key: "last_login_at", header: "Last login", render: (o) => <Timestamp value={o.last_login_at} /> },
    { key: "created_at", header: "Created", render: (o) => <Timestamp value={o.created_at} /> },
  ];

  return (
    <div>
      {isLoading && <p>Loading operators...</p>}
      {error && <ErrorNotice error={error} />}
      {data && <DataTable columns={columns} rows={data.items} getRowKey={(o) => o.id} emptyMessage="No operators." />}
    </div>
  );
}

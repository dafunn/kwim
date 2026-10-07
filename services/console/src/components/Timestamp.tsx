interface TimestampProps {
  value: string | null | undefined;
}

/** Renders in the browser's zone, with the UTC value available on hover. */
export function Timestamp({ value }: TimestampProps) {
  if (!value) return <span>-</span>;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return <span>{value}</span>;
  return <time dateTime={value} title={date.toISOString()}>{date.toLocaleString()}</time>;
}

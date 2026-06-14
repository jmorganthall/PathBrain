// Formatting helpers for numbers and timestamps.

export function fmtMs(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${value.toFixed(1)} ms`;
}

export function fmtNum(value: number | null | undefined, digits = 1): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

export function fmtScore(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return Math.round(value).toString();
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtTimeShort(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function metricValue(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return Number.isInteger(v) ? v.toString() : v.toFixed(1);
}

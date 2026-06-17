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

// Backend timestamps are UTC but may arrive without a timezone designator
// (SQLite drops tzinfo). Treat a bare timestamp as UTC so the browser renders it
// in the viewer's own local timezone instead of assuming it's already local.
export function parseApiDate(iso: string): Date {
  const hasTz = /([zZ])$|([+-]\d{2}:?\d{2})$/.test(iso.trim());
  return new Date(hasTz ? iso : `${iso}Z`);
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = parseApiDate(iso);
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
  const d = parseApiDate(iso);
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

// Human-friendly duration from milliseconds, e.g. "850 ms", "12s", "2m 05s".
export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const totalSeconds = ms / 1000;
  if (totalSeconds < 60) return `${totalSeconds.toFixed(totalSeconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = Math.round(totalSeconds % 60);
  return `${minutes}m ${seconds.toString().padStart(2, "0")}s`;
}

// Estimated time remaining for an in-progress run, in ms (may be negative when
// the run is overdue). Uses the run's own measured per-iteration time once it has
// one, else the supplied estimate. Returns null when we can't estimate.
export function runRemainingMs(
  startedAt: string | null | undefined,
  iterations: number | null | undefined,
  perIterationMs: number | null | undefined,
  now: number,
): number | null {
  if (!startedAt || perIterationMs == null) return null;
  const started = parseApiDate(startedAt).getTime();
  if (Number.isNaN(started)) return null;
  const total = perIterationMs * Math.max(iterations || 1, 1);
  return total - (now - started);
}

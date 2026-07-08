// Shared "selectable numeric field" model for the Settings-Impact page: one list
// powers both the dynamic quadrant's X/Y axis pickers and the table's column
// selector. Fields come from two sources — the response's `fields` descriptor (axis
// scores + run stats) and every metric actually present in the profiles' `metrics`
// maps (labels/units/direction from the /api/metrics catalog via useMetricMeta).
import type { MetricMeta } from "./metrics";
import type { ProfileField, SettingsProfile } from "../api/types";

export interface FieldDef {
  key: string;
  label: string;
  unit: string;
  higherIsBetter: boolean;
  group: string;
  get: (p: SettingsProfile) => number | null;
}

// Read any numeric field off a profile by key (axis score, run stat, or metric).
// A ``crown:<metric>`` key resolves to that metric's field-normalized raw value (0–100,
// higher = better) — the exact quantity the Overall corners over — distinct from the raw
// ``<metric>`` value (e.g. FCP in ms, lower = better) so both can coexist as columns.
export function profileValue(p: SettingsProfile, key: string): number | null {
  switch (key) {
    case "overall":
      return p.overall;
    case "iterations":
      return p.iterations;
    case "count":
      return p.count;
    case "relative_overall":
      // "vs typical" = the time-adjusted **Overall** edge (the axis we crown on today),
      // matching the pinned column — NOT the legacy smoothness-based relative_sops.
      return p.relative_overall?.delta_median ?? null;
    case "weather_adjusted_overall":
      // Display-only metric-based "vs weather": the Overall re-cornered over setup-stripped
      // fcp/lcp (per-run, self-contained). Same 0–100 space as Overall; not a crown input.
      return p.weather_adjusted_overall ?? null;
  }
  if (key.startsWith("crown:")) return p.crown_norm?.[key.slice(6)] ?? null;
  if (p.scores && key in p.scores) return p.scores[key];
  return p.metrics?.[key] ?? null;
}

// Build the full selectable field list (response fields first, then metric fields).
export function buildFields(
  profiles: SettingsProfile[],
  responseFields: ProfileField[],
  meta: (key: string) => MetricMeta,
): FieldDef[] {
  const out: FieldDef[] = responseFields.map((f) => ({
    key: f.key,
    label: f.label,
    unit: f.unit,
    higherIsBetter: f.higher_is_better,
    group: f.group,
    get: (p: SettingsProfile) => profileValue(p, f.key),
  }));

  const metricKeys = new Set<string>();
  for (const p of profiles) for (const k of Object.keys(p.metrics ?? {})) metricKeys.add(k);
  for (const key of [...metricKeys].sort()) {
    const m = meta(key);
    out.push({
      key,
      label: m.label,
      unit: m.unit ?? "",
      higherIsBetter: m.higherIsBetter ?? false,
      group: "Metrics",
      get: (p: SettingsProfile) => p.metrics?.[key] ?? null,
    });
  }
  return out;
}

// Format a field value for display, by unit.
export function fmtFieldValue(value: number | null | undefined, unit: string): string {
  if (value == null) return "—";
  const rounded = Math.abs(value) >= 100 ? Math.round(value) : Math.round(value * 100) / 100;
  if (unit === "%") return `${rounded}%`;
  if (unit === "score") return `${rounded}`;
  return unit ? `${rounded} ${unit}` : `${rounded}`;
}

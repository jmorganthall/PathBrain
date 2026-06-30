// Rank profiles by a (higher-is-better) score and map a rank to a green→red colour.
// Used by the Settings-Impact table and the Profile page so the axis/Overall columns
// read as standings ("1 = best") instead of raw 0–100 measurements.
import type { SettingsProfile } from "../api/types";
import { profileValue } from "./profileFields";

export interface MetricRanking {
  // fingerprint → 1-based rank (1 = best). Competition ranking: ties share a rank and
  // the next rank skips (1, 2, 2, 4).
  rankByFp: Record<string, number>;
  // How many profiles actually have a value for this metric (the "of N" denominator).
  total: number;
}

// All current axis scores are higher-is-better, so the highest value is rank 1.
export function rankByMetric(profiles: SettingsProfile[], key: string): MetricRanking {
  const withVal = profiles
    .map((p) => ({ fp: p.fingerprint, v: profileValue(p, key) }))
    .filter((x): x is { fp: string; v: number } => x.v != null);
  const sorted = [...withVal].sort((a, b) => b.v - a.v);
  const rankByFp: Record<string, number> = {};
  let rank = 0;
  let seen = 0;
  let prev: number | null = null;
  for (const it of sorted) {
    seen += 1;
    if (prev === null || it.v < prev) {
      rank = seen; // ties keep the previous rank; a strictly-worse value jumps to its position
      prev = it.v;
    }
    rankByFp[it.fp] = rank;
  }
  return { rankByFp, total: withVal.length };
}

// rank 1 → green, last → red, linearly through amber. A single-profile pool is "best".
export function rankColor(rank: number | null | undefined, total: number): string {
  if (rank == null) return "#90a4ae"; // no value → neutral grey
  if (total <= 1) return "#66bb6a";
  const frac = (rank - 1) / (total - 1); // 0 = best … 1 = worst
  const hue = 120 * (1 - frac); // 120° green → 0° red
  return `hsl(${Math.round(hue)}, 70%, 55%)`;
}

// "1 / 12" style standing, or an em dash when unranked.
export function rankLabel(rank: number | null | undefined, total: number): string {
  if (rank == null) return "—";
  return `${rank} / ${total}`;
}

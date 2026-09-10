// Reading the ring for a person.
//
// A live ring board carries everything the verdict is decided on — round tallies, the
// median margin, the streak, the peek-corrected p-value — and the reported failure was
// that a human could not tell from it how the profiles in the ring stand against each
// other. The numbers were all there; the *reading* was left to the viewer. These helpers
// turn each seat's board into the three things a person actually asks — is it ahead or
// behind the belt, by how much, and is that settled yet — and put every profile in the
// ring, belt included, on ONE scale (Overall points relative to the belt) so their
// relationship is a picture rather than four numbers per row.
//
// Pure functions over the live payload: nothing here reads the API or changes a score.
import type { DuelLeg, DuelLive } from "../api/types";

export type Standing = "ahead" | "behind" | "level" | "unmeasured";
export type Verdict = "called" | "too_early" | "unproven" | "inside_floor" | "nothing";

export interface SeatReading {
  fingerprint: string | null;
  name: string;
  /** Median per-round margin, challenger − belt, in Overall points. */
  margin: number | null;
  /** The spread of the rounds so far (min..max of the margins the board carries). */
  lo: number | null;
  hi: number | null;
  rounds: number;
  standing: Standing;
  verdict: Verdict;
  /** The reading in one sentence, from this seat's side. */
  sentence: string;
}

const fmt = (v: number, digits = 2) => v.toFixed(digits);

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function seatName(board: DuelLive): string {
  return board.challenger.name || board.challenger.label || "challenger";
}

/** Read one seat's board into a standing, a verdict and a sentence. */
export function readSeat(board: DuelLive, beltName: string): SeatReading {
  const name = seatName(board);
  const rounds = board.pairs;
  const margin = board.median_margin;
  const margins = board.margins ?? [];
  const lo = margins.length ? Math.min(...margins) : null;
  const hi = margins.length ? Math.max(...margins) : null;

  let standing: Standing = "unmeasured";
  if (rounds > 0 && margin != null) standing = margin > 0 ? "ahead" : margin < 0 ? "behind" : "level";

  let verdict: Verdict = "nothing";
  if (rounds > 0 && margin != null) {
    const proven = board.p_value != null && board.p_value <= board.alpha;
    const streakEnds = board.streak.length >= board.streak.needed && board.streak.length > 0;
    if (Math.abs(margin) < board.min_margin && board.min_margin > 0) verdict = "inside_floor";
    else if (proven || streakEnds) verdict = "called";
    else if (rounds < board.min_pairs) verdict = "too_early";
    else verdict = "unproven";
  }

  const sentence = (() => {
    if (standing === "unmeasured") {
      return `${name} has no rounds against ${beltName} yet.`;
    }
    const gap = margin == null ? "" : ` by about ${fmt(Math.abs(margin))} Overall points`;
    const lead =
      standing === "level"
        ? `${name} is dead level with ${beltName}`
        : standing === "ahead"
          ? `${name} is ahead of ${beltName}${gap}`
          : `${name} is behind ${beltName}${gap}`;
    const after = ` after ${plural(rounds, "round")}`;
    const streakSide =
      board.streak.side === "challenger" ? name : board.streak.side === "incumbent" ? beltName : null;
    const streakLeft = board.streak.needed - board.streak.length;
    const streakNote =
      streakSide && board.streak.length > 0 && streakLeft > 0
        ? ` ${streakLeft === 1 ? "One more" : `${streakLeft} more`} straight win${streakLeft === 1 ? "" : "s"} for ${streakSide} would end it on the spot.`
        : "";
    switch (verdict) {
      case "inside_floor":
        return `${lead}${after} — inside the ${fmt(board.min_margin, 1)}-point floor, so as it stands this is a draw.`;
      case "called":
        return `${lead}${after} — enough to call it; the match ends at the next seam.`;
      case "too_early":
        return `${lead}${after} — too early to call (a call needs ${board.min_pairs}).${streakNote}`;
      case "unproven":
        return `${lead}${after} — not proven yet${
          board.p_value != null ? ` (p ${fmt(board.p_value, 3)}, needs ≤ ${fmt(board.alpha, 3)})` : ""
        }; the margins are still inside the run-to-run noise.${streakNote}`;
      default:
        return `${lead}${after}.`;
    }
  })();

  return { fingerprint: board.challenger.fingerprint, name, margin, lo, hi, rounds, standing, verdict, sentence };
}

export interface RingOrderEntry {
  name: string;
  fingerprint: string | null;
  /** Overall points relative to the belt (the belt itself is 0). */
  relative: number;
  isBelt: boolean;
}

/**
 * Every profile in the ring on one scale, best first. Seats are only ever measured
 * against the belt, so two seats' order against EACH OTHER is inferred through it —
 * `inferred` says so whenever that inference is being made.
 */
export function ringOrder(
  seats: SeatReading[],
  belt: { name: string; fingerprint: string | null },
): { order: RingOrderEntry[]; inferred: boolean } {
  const measured = seats.filter((s) => s.margin != null);
  const order: RingOrderEntry[] = [
    { name: belt.name, fingerprint: belt.fingerprint, relative: 0, isBelt: true },
    ...measured.map((s) => ({ name: s.name, fingerprint: s.fingerprint, relative: s.margin as number, isBelt: false })),
  ].sort((a, b) => b.relative - a.relative);
  return { order, inferred: measured.length >= 2 };
}

/** The span of the relative scale needed to show every seat's rounds, never under ±1 point. */
export function ringSpan(seats: SeatReading[]): number {
  let span = 1;
  for (const s of seats) {
    for (const v of [s.margin, s.lo, s.hi]) if (v != null) span = Math.max(span, Math.abs(v));
  }
  return span * 1.15;
}

/** Median leg Overall per profile over the legs the board carries (usable legs only). */
export function legMedians(legs: DuelLeg[]): Map<string, { median: number; n: number }> {
  const byFp = new Map<string, number[]>();
  for (const leg of legs) {
    if (leg.overall == null) continue;
    const arr = byFp.get(leg.fingerprint) ?? [];
    arr.push(leg.overall);
    byFp.set(leg.fingerprint, arr);
  }
  const out = new Map<string, { median: number; n: number }>();
  for (const [fp, values] of byFp) {
    const s = [...values].sort((a, b) => a - b);
    const mid = Math.floor(s.length / 2);
    const median = s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
    out.set(fp, { median, n: s.length });
  }
  return out;
}

import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { FastestLegs, LegGaps } from "../api/types";

// The "vs fastest" reading for profiles a view names but whose payload does not carry it
// (the Duels champion, the Auto-apply popover). One request for every fingerprint asked;
// best-effort — a failed read renders nothing rather than a broken view.
export function useFastestGaps(fingerprints: (string | null | undefined)[]): {
  fastest: FastestLegs | null;
  gaps: Record<string, LegGaps>;
} {
  const key = Array.from(new Set(fingerprints.filter((f): f is string => !!f))).sort().join(",");
  const [state, setState] = useState<{ fastest: FastestLegs | null; gaps: Record<string, LegGaps> }>({
    fastest: null,
    gaps: {},
  });
  useEffect(() => {
    if (!key) return;
    let live = true;
    api
      .fastestGaps(key.split(","))
      .then((r) => live && setState({ fastest: r.fastest, gaps: r.gaps || {} }))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [key]);
  return state;
}

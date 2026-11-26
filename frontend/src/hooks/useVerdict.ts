import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { VerdictNow } from "../api/types";

// The crown as the Verdict names it: the profile Auto-apply follows, and why. Every view
// that marks a crown reads it here rather than taking a "best" from its own payload.
// Best-effort — null while loading or on a failed read, so a view marks no crown rather
// than a wrong one.
export function useVerdict(): VerdictNow | null {
  const [now, setNow] = useState<VerdictNow | null>(null);
  useEffect(() => {
    let live = true;
    api
      .verdict(1)
      .then((v) => live && setNow(v.verdict))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);
  return now;
}

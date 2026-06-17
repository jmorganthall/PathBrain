import { useEffect, useState } from "react";

/**
 * Returns a `Date.now()` value that ticks on an interval while `active` is true,
 * so countdowns (e.g. run ETAs) update live. Idle (no timer) when inactive.
 */
export function useNow(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [active, intervalMs]);
  return now;
}

// The current methodology (`GET /api/methodologies/current`), fetched once and shared.
// Screens that name axes, metrics or the Overall's makeup read them from here rather than
// from fixed lists, so they follow the methodology when it changes. Until it loads (or if
// it fails) `useMethodology()` returns null and callers fall back to what the response
// they already have says.
import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";

import { api } from "../api/client";
import type { MethodologyDetail, MethodologyMetric } from "../api/types";

const MethodologyContext = createContext<MethodologyDetail | null>(null);

export function MethodologyProvider({ children }: { children: ReactNode }) {
  const [current, setCurrent] = useState<MethodologyDetail | null>(null);

  useEffect(() => {
    let active = true;
    api
      .methodologyCurrent()
      .then((m) => {
        if (active) setCurrent(m);
      })
      .catch(() => {
        /* stay null; callers fall back */
      });
    return () => {
      active = false;
    };
  }, []);

  return <MethodologyContext.Provider value={current}>{children}</MethodologyContext.Provider>;
}

/** The current methodology, or null until it has loaded. */
export function useMethodology(): MethodologyDetail | null {
  return useContext(MethodologyContext);
}

/** The Overall's metric keys and each one's share of it (shares sum to 1). */
export function overallShares(m: MethodologyDetail | null): { key: string; share: number }[] {
  const overall = m?.definition.overall;
  if (!overall || overall.metrics.length === 0) return [];
  const raw = overall.metrics.map((k) => overall.weights?.[k] ?? 1);
  const total = raw.reduce((a, b) => a + b, 0) || 1;
  return overall.metrics.map((key, i) => ({ key, share: raw[i] / total }));
}

/** "FCP, LCP and Cadence CoV" from the methodology's own labels. */
export function joinLabels(keys: string[], metrics: MethodologyMetric[] | undefined): string {
  const byKey = new Map((metrics ?? []).map((x) => [x.key, x.label]));
  const names = keys.map((k) => byKey.get(k) ?? k);
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

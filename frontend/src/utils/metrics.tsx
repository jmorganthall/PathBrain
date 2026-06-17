// Metric display metadata, sourced from the backend metric registry
// (`GET /api/metrics`) so there's a single source of truth. The provider fetches
// the catalog once; `useMetricMeta()` returns a lookup that works for either the
// logical key ("fcp") or the raw plugin key ("fcp_ms"). `useMetricOrder()` gives
// the chronological display rank. Until the catalog loads (or if it fails),
// lookups fall back to sensible defaults so the UI never breaks.
import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { api } from "../api/client";

export interface MetricMeta {
  label: string;
  description: string;
  unit?: string;
  higherIsBetter?: boolean;
}

interface Catalog {
  meta: Record<string, MetricMeta>;
  order: Record<string, number>;
}

const EMPTY: Catalog = { meta: {}, order: {} };
const MetricCatalogContext = createContext<Catalog>(EMPTY);

export function MetricCatalogProvider({ children }: { children: ReactNode }) {
  const [catalog, setCatalog] = useState<Catalog>(EMPTY);

  useEffect(() => {
    let active = true;
    api
      .metrics()
      .then((res) => {
        if (!active) return;
        const meta: Record<string, MetricMeta> = {};
        const order: Record<string, number> = {};
        for (const e of res.metrics) {
          const m: MetricMeta = {
            label: e.label,
            description: e.description,
            unit: e.unit || undefined,
            higherIsBetter: e.higher_is_better,
          };
          // Index by both the logical key and the raw plugin key so callers can
          // look up by whichever they have.
          meta[e.key] = m;
          meta[e.source_key] = m;
          order[e.key] = e.order;
          order[e.source_key] = e.order;
        }
        setCatalog({ meta, order });
      })
      .catch(() => {
        /* keep empty catalog; lookups fall back to defaults */
      });
    return () => {
      active = false;
    };
  }, []);

  return <MetricCatalogContext.Provider value={catalog}>{children}</MetricCatalogContext.Provider>;
}

function fallback(key: string): MetricMeta {
  return { label: key, description: "Measured value for this run." };
}

/** Returns a lookup `(key) => MetricMeta`, resolving by logical or raw plugin key. */
export function useMetricMeta(): (key: string) => MetricMeta {
  const { meta } = useContext(MetricCatalogContext);
  return useMemo(() => (key: string) => meta[key] ?? fallback(key), [meta]);
}

/** Returns a lookup `(key) => order rank` (lower = earlier; unknown keys sort last). */
export function useMetricOrder(): (key: string) => number {
  const { order } = useContext(MetricCatalogContext);
  return useMemo(() => (key: string) => order[key] ?? Number.MAX_SAFE_INTEGER, [order]);
}

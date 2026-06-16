// Metric display metadata, sourced from the backend metric registry
// (`GET /api/metrics`) so there's a single source of truth. The provider fetches
// the catalog once; `useMetricMeta()` returns a lookup that works for either the
// logical key ("fcp") or the raw plugin key ("fcp_ms"). Until the catalog loads
// (or if it fails), lookups fall back to a sensible default so the UI never breaks.
import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { api } from "../api/client";

export interface MetricMeta {
  label: string;
  description: string;
  unit?: string;
  higherIsBetter?: boolean;
}

type MetaMap = Record<string, MetricMeta>;

const MetricCatalogContext = createContext<MetaMap>({});

export function MetricCatalogProvider({ children }: { children: ReactNode }) {
  const [map, setMap] = useState<MetaMap>({});

  useEffect(() => {
    let active = true;
    api
      .metrics()
      .then((res) => {
        if (!active) return;
        const next: MetaMap = {};
        for (const e of res.metrics) {
          const meta: MetricMeta = {
            label: e.label,
            description: e.description,
            unit: e.unit || undefined,
            higherIsBetter: e.higher_is_better,
          };
          // Index by both the logical key and the raw plugin key so callers can
          // look up by whichever they have.
          next[e.key] = meta;
          next[e.source_key] = meta;
        }
        setMap(next);
      })
      .catch(() => {
        /* keep empty map; lookups fall back to the default */
      });
    return () => {
      active = false;
    };
  }, []);

  return <MetricCatalogContext.Provider value={map}>{children}</MetricCatalogContext.Provider>;
}

function fallback(key: string): MetricMeta {
  return { label: key, description: "Measured value for this run." };
}

/** Returns a lookup `(key) => MetricMeta`, resolving by logical or raw plugin key. */
export function useMetricMeta(): (key: string) => MetricMeta {
  const map = useContext(MetricCatalogContext);
  return useMemo(() => (key: string) => map[key] ?? fallback(key), [map]);
}

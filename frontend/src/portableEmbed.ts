// The hook PathBrain's own `portable` plugin drives: it loads `/away?embedded=1` in the
// server's Chromium and calls `window.__pathbrainPortable.runOne(recipe)` — the SAME code a
// phone runs — so the NAS-run home reference and a phone run are one instrument. Registered
// from the app entry so it is present on every route; the runner itself is loaded lazily so
// nothing is added to the main bundle until the plugin (or the Away page) asks for it.
import type { PortableRecipe, PortableRawIteration } from "./api/types";

declare global {
  interface Window {
    __pathbrainPortable?: {
      ready: boolean;
      runOne: (recipe: PortableRecipe) => Promise<PortableRawIteration>;
      clientInfo: () => Promise<Record<string, unknown>>;
    };
  }
}

if (typeof window !== "undefined") {
  window.__pathbrainPortable = {
    ready: true,
    runOne: async (recipe) => (await import("./utils/portableTest")).runPortableIteration(recipe),
    clientInfo: async () => (await import("./utils/portableTest")).clientInfo(),
  };
}

export {};

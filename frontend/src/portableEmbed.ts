// The hook PathBrain's own `portable` plugin drives: it loads `/away?embedded=1` in the
// server's Chromium and calls `window.__pathbrainPortable.run(recipe)` — the SAME code, in
// the SAME sequence, a phone runs (`runPortableTest`: a warm-up fetch, then the recipe's
// iterations in one page) — so the NAS-run home reference and a phone run are one instrument
// at one connection warmth. `runOne` (a single cold iteration, no warm-up) is kept for
// callers that want exactly that; it is not what the reference is built from any more, since
// a cold context measured against a phone's warm tab read as the phone "beating" the wire on
// every setup-bound metric. Registered from the app entry so it is present on every route;
// the runner itself is loaded lazily so nothing is added to the main bundle until the plugin
// (or the Away page) asks for it.
import type { PortableRaw, PortableRecipe, PortableRawIteration } from "./api/types";

declare global {
  interface Window {
    __pathbrainPortable?: {
      ready: boolean;
      run: (recipe: PortableRecipe) => Promise<PortableRaw>;
      runOne: (recipe: PortableRecipe) => Promise<PortableRawIteration>;
      clientInfo: () => Promise<Record<string, unknown>>;
    };
  }
}

if (typeof window !== "undefined") {
  window.__pathbrainPortable = {
    ready: true,
    run: async (recipe) =>
      (await import("./utils/portableTest")).runPortableTest(recipe, { iterations: recipe.iterations }),
    runOne: async (recipe) => (await import("./utils/portableTest")).runPortableIteration(recipe),
    clientInfo: async () => (await import("./utils/portableTest")).clientInfo(),
  };
}

export {};

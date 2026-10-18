// The vocabulary, defined once.
//
// PathBrain has a working language — profile, crown, ring, belt, weather, pipeline, lever —
// that every page uses and no page defines. A reader who knows what the app is for still
// meets "the belt holder defends against an heir under shared weather" on their first
// visit. Each term is defined here in one plain sentence (two at most), and two surfaces
// read the map: the Glossary dialog behind the top-bar "?" button, and `<Term>` (a dotted
// underline with the definition as its tooltip) wherever a page's lead sentence has to use
// one of these words. Definitions describe what the app does today; they never change it.

export interface GlossaryEntry {
  /** Short key used by `<Term id=…>`. */
  id: string;
  /** The word as it appears in the UI. */
  term: string;
  /** One or two plain sentences. */
  definition: string;
  /** Other spellings the UI uses for the same thing. */
  aka?: string[];
  /** Where to read more. */
  to?: string;
}

export const GLOSSARY: GlossaryEntry[] = [
  {
    id: "profile",
    term: "Profile",
    definition:
      "One distinct set of firewall shaper settings. Every measurement is filed under the profile the firewall was on at the time, so profiles are what get ranked.",
    aka: ["configuration", "settings profile"],
    to: "/settings",
  },
  {
    id: "overall",
    term: "Overall",
    definition:
      "The headline 0–100 score a profile is ranked on; higher is better. It is built from the few page-load measurements that best track how fast a page feels, as set by the current methodology.",
    to: "/methodology",
  },
  {
    id: "run",
    term: "Run",
    definition:
      "One benchmark: the measurement suite repeated a chosen number of times (iterations), with the median taken as the result.",
    to: "/history",
  },
  {
    id: "iteration",
    term: "Iteration",
    definition:
      "One pass through the whole measurement suite. Iterations, not runs, are how much evidence a profile has.",
  },
  {
    id: "confident",
    term: "Confident",
    definition:
      "A profile with enough measured iterations to be ranked. Below that bar a profile is shown but never crowned.",
  },
  {
    id: "crown",
    term: "Crown / best profile",
    definition:
      "The confident profile with the highest Overall across every run it has ever had. There is no minimum margin: better by a hair is still better.",
    aka: ["pooled crown", "best"],
    to: "/overall",
  },
  {
    id: "pooled",
    term: "Pooled",
    definition:
      "A profile's whole record averaged together, every run in whatever conditions it happened to meet. Broad, but profiles are compared across different times and weather.",
  },
  {
    id: "duel",
    term: "Duel",
    definition:
      "Two profiles measured back to back, over and over, so both meet the same network conditions. A match is two profiles; a round is one paired measurement; a session is a night of matches.",
    aka: ["the ring", "match", "round", "bout", "pair"],
    to: "/duels",
  },
  {
    id: "belt",
    term: "Belt / champion",
    definition:
      "The profile that currently holds the duel title. It defends every match; a challenger that beats it takes the belt.",
    to: "/duels",
  },
  {
    id: "rating",
    term: "Rating",
    definition:
      "A strength fitted from every duel result, on an Elo-like scale where 1500 is the middle of the field. Beating a strong profile moves it a lot; beating a weak one barely.",
    aka: ["proven"],
    to: "/duels",
  },
  {
    id: "fused",
    term: "Overall ranking (fused)",
    definition:
      "One ranking that combines the pooled record with the duel rounds, weighting each by how much it can be trusted. It is what Auto-apply follows by default.",
    to: "/overall",
  },
  {
    id: "tied",
    term: "Tied",
    definition:
      "Within measurement noise of the leader. A tie is a note on the ranking, never a shared rank: the higher score still wins.",
  },
  {
    id: "heirs",
    term: "Heirs / contenders",
    definition:
      "Under-measured profiles whose best case could still beat the best profile. Measuring them is how the ranking gets settled.",
    to: "/settings",
  },
  {
    id: "weather",
    term: "Weather",
    definition:
      "Network conditions the shaper cannot change: DNS, connection and handshake times, measured on every run. Duels cancel it out by measuring both sides at the same time.",
    to: "/weather",
  },
  {
    id: "pipeline",
    term: "Pipeline",
    definition:
      "PathBrain measures one thing at a time; the pipeline is that single slot. Busy means something is measuring now, and anything you start queues behind it.",
  },
  {
    id: "lever",
    term: "Lever",
    definition:
      "One shaper setting on its own: quantum, target, interval, limit, ECN or bandwidth. A lever duel measures what moving one of them does.",
    to: "/levers",
  },
  {
    id: "sqm",
    term: "SQM / shaper",
    definition:
      "The firewall's traffic shaper (fq_codel). \"SQM off\" or \"No Shaper\" is the unshaped link, the baseline every shaped profile is measured against.",
    to: "/baseline",
  },
  {
    id: "methodology",
    term: "Methodology",
    definition:
      "The versioned recipe that turns raw measurements into scores: which metrics count, their weights and thresholds, and the sites measured. Changing any of it publishes a new version.",
    to: "/methodology",
  },
  {
    id: "live",
    term: "Live / on the firewall",
    definition: "The profile the firewall is set to right now.",
  },
  {
    id: "fingerprint",
    term: "Fingerprint / call sign",
    definition:
      "A profile's identity hash, and the readable two-word name derived from it (\"Speedy Sloth\"). Names can be edited on the profile page.",
  },
];

export const GLOSSARY_BY_ID: Record<string, GlossaryEntry> = Object.fromEntries(
  GLOSSARY.map((e) => [e.id, e]),
);

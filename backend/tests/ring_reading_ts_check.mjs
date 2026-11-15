// Runs frontend/src/utils/ringReading.ts's readSeat over the shared fixture table and prints
// each case's standing, verdict and sentence as JSON. Run by tests/test_ring_reading.py as
// `node --input-type=module -` from frontend/ (so esbuild resolves from its node_modules),
// with RING_TS and RING_CASES naming the two files. RING_LOCAL=1 strips `reading` first.
import { build } from "esbuild";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const cases = JSON.parse(readFileSync(process.env.RING_CASES, "utf8"));
const out = join(mkdtempSync(join(tmpdir(), "ring-")), "ringReading.mjs");
await build({ entryPoints: [process.env.RING_TS], bundle: true, format: "esm", platform: "node", outfile: out, logLevel: "silent" });
const mod = await import(pathToFileURL(out).href);
const rows = cases.map((c) => {
  const board = { ...c.board };
  if (process.env.RING_LOCAL === "1") delete board.reading;
  const r = mod.readSeat(board, "Belt");
  return { name: c.name, standing: r.standing, verdict: r.verdict, sentence: r.sentence };
});
writeFileSync(1, JSON.stringify(rows));

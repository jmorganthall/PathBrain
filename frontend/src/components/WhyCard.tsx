import { useEffect, useMemo, useState } from "react";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import FormControl from "@mui/material/FormControl";
import InputLabel from "@mui/material/InputLabel";
import MenuItem from "@mui/material/MenuItem";
import Select from "@mui/material/Select";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import { api } from "../api/client";
import type { ProfileWhy, SettingsProfile, WhyLeg, WhyPhase, WhySite } from "../api/types";
import { FoldCard, HelpTip } from "./Explain";

/**
 * Where a win lives — what the Overall gap between this profile and a reference is MADE of.
 *
 * The standings say which profile wins and by how much; this card says where the margin
 * comes from: per crown leg (exact under the weighted crown — the leg points add up to the
 * gap), per navigation phase (which part of the load moved), and per site (is the edge
 * everywhere, or one page). Every delta carries the same noise bar the crown uses to call
 * a tie. Read-only; a second reading of the same runs, never a re-score.
 */

const DEFAULT_VS = "__default__";

function signed(v: number | null | undefined, digits = 1, unit = ""): string {
  if (v == null || Number.isNaN(v)) return "—";
  const s = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${s}${Math.abs(v).toFixed(digits)}${unit}`;
}

function ClearChip({ clear, se }: { clear: boolean | null; se: number | null }) {
  if (clear == null) {
    return <Chip size="small" variant="outlined" label="noise unknown" />;
  }
  return (
    <Tooltip title={se != null ? `± ${se.toFixed(1)} (standard error of the median, pooled)` : ""}>
      <Chip
        size="small"
        color={clear ? "success" : "default"}
        variant={clear ? "filled" : "outlined"}
        label={clear ? "clear" : "within noise"}
      />
    </Tooltip>
  );
}

/** A signed bar: fills right of centre for points in this profile's favour, left against. */
function PointsBar({ points, max }: { points: number | null; max: number }) {
  if (points == null) return <Box sx={{ height: 8 }} />;
  const share = max > 0 ? Math.min(1, Math.abs(points) / max) : 0;
  const width = `${(share * 50).toFixed(1)}%`;
  return (
    <Box sx={{ position: "relative", height: 8, bgcolor: "action.hover", borderRadius: 1, overflow: "hidden" }}>
      <Box sx={{ position: "absolute", left: "50%", top: 0, bottom: 0, width: 1, bgcolor: "divider" }} />
      <Box
        sx={{
          position: "absolute",
          top: 0,
          bottom: 0,
          width,
          ...(points >= 0 ? { left: "50%" } : { right: "50%" }),
          bgcolor: points >= 0 ? "success.main" : "error.main",
          borderRadius: 1,
        }}
      />
    </Box>
  );
}

function LegsTable({ legs, bName }: { legs: WhyLeg[]; bName: string }) {
  const max = Math.max(0.01, ...legs.map((l) => Math.abs(l.points ?? 0)));
  return (
    <TableContainer sx={{ overflowX: "auto" }}>
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Crown leg</TableCell>
            <TableCell align="right">This profile</TableCell>
            <TableCell align="right">{bName}</TableCell>
            <TableCell align="right">Δ measured</TableCell>
            <TableCell align="right">Points</TableCell>
            <TableCell sx={{ minWidth: 120 }} />
            <TableCell />
          </TableRow>
        </TableHead>
        <TableBody>
          {legs.map((l) => (
            <TableRow key={l.metric}>
              <TableCell>
                <Typography variant="body2">{l.label}</Typography>
                <Typography variant="caption" color="text.secondary">
                  weight {l.weight} · {Math.round(l.share_of_weight * 100)}% of the crown
                </Typography>
              </TableCell>
              <TableCell align="right">
                <Typography variant="body2">{l.a.raw != null ? `${l.a.raw.toFixed(0)} ${l.unit}` : "—"}</Typography>
                <Typography variant="caption" color="text.secondary">
                  score {l.a.subscore != null ? l.a.subscore.toFixed(1) : "—"} · n={l.a.n}
                </Typography>
              </TableCell>
              <TableCell align="right">
                <Typography variant="body2">{l.b.raw != null ? `${l.b.raw.toFixed(0)} ${l.unit}` : "—"}</Typography>
                <Typography variant="caption" color="text.secondary">
                  score {l.b.subscore != null ? l.b.subscore.toFixed(1) : "—"} · n={l.b.n}
                </Typography>
              </TableCell>
              <TableCell align="right">
                <Typography variant="body2" color={l.delta_raw != null && l.delta_raw < 0 ? "success.main" : l.delta_raw ? "error.main" : "text.secondary"}>
                  {signed(l.delta_raw, 0, ` ${l.unit}`)}
                </Typography>
              </TableCell>
              <TableCell align="right">
                <Typography variant="body2" sx={{ fontWeight: 700 }}>
                  {signed(l.points, 2)}
                </Typography>
              </TableCell>
              <TableCell>
                <PointsBar points={l.points} max={max} />
              </TableCell>
              <TableCell>
                {l.missing ? <Chip size="small" variant="outlined" label="missing on a side" /> : <ClearChip clear={l.clear} se={l.se} />}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function PhasesTable({ phases, bName }: { phases: WhyPhase[]; bName: string }) {
  const rows = phases.filter((p) => p.a.n > 0 || p.b.n > 0);
  if (rows.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        No navigation-phase readings on these runs.
      </Typography>
    );
  }
  return (
    <TableContainer sx={{ overflowX: "auto" }}>
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Phase of the load</TableCell>
            <TableCell align="right">This profile</TableCell>
            <TableCell align="right">{bName}</TableCell>
            <TableCell align="right">Δ</TableCell>
            <TableCell />
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((p) => (
            <TableRow key={p.metric}>
              <TableCell>{p.label}</TableCell>
              <TableCell align="right">{p.a.median != null ? `${p.a.median.toFixed(0)} ms` : "—"}</TableCell>
              <TableCell align="right">{p.b.median != null ? `${p.b.median.toFixed(0)} ms` : "—"}</TableCell>
              <TableCell align="right">
                <Typography variant="body2" color={p.delta != null && p.delta < 0 ? "success.main" : p.delta ? "error.main" : "text.secondary"}>
                  {signed(p.delta, 0, " ms")}
                </Typography>
              </TableCell>
              <TableCell>
                <ClearChip clear={p.clear} se={p.se} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function SitesTable({ sites, legs }: { sites: WhySite[]; legs: WhyLeg[] }) {
  if (sites.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        No page both profiles have loaded on recent runs, so nothing can be priced per site.
      </Typography>
    );
  }
  return (
    <TableContainer sx={{ overflowX: "auto" }}>
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Site</TableCell>
            <TableCell align="right">Points if this site alone</TableCell>
            {legs.map((l) => (
              <TableCell key={l.metric} align="right">
                Δ {l.label}
              </TableCell>
            ))}
            <TableCell>Where in the load</TableCell>
            <TableCell align="right">Runs</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {sites.map((s) => (
            <TableRow key={s.url}>
              <TableCell>
                <Tooltip title={s.url}>
                  <Typography variant="body2" noWrap sx={{ maxWidth: 220 }}>
                    {s.host}
                  </Typography>
                </Tooltip>
              </TableCell>
              <TableCell align="right">
                <Typography
                  variant="body2"
                  sx={{ fontWeight: 700 }}
                  color={s.points == null ? "text.secondary" : s.points > 0 ? "success.main" : s.points < 0 ? "error.main" : "text.secondary"}
                >
                  {signed(s.points, 2)}
                </Typography>
              </TableCell>
              {legs.map((l) => {
                const leg = s.legs.find((x) => x.metric === l.metric);
                return (
                  <TableCell key={l.metric} align="right">
                    <Typography variant="body2" color={leg?.clear ? (leg.delta != null && leg.delta < 0 ? "success.main" : "error.main") : "text.secondary"}>
                      {signed(leg?.delta, 0, " ms")}
                      {leg?.clear ? " ✓" : ""}
                    </Typography>
                  </TableCell>
                );
              })}
              <TableCell>
                {s.top_phase ? (
                  <Typography variant="caption">
                    {s.top_phase.label}: {signed(s.top_phase.delta, 0, " ms")}
                  </Typography>
                ) : (
                  <Typography variant="caption" color="text.secondary">
                    no phase clear
                  </Typography>
                )}
              </TableCell>
              <TableCell align="right">
                <Typography variant="caption" color="text.secondary">
                  {s.runs_a} / {s.runs_b}
                </Typography>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

export default function WhyCard({
  fingerprint,
  profiles,
  bestFp,
}: {
  fingerprint: string;
  profiles: SettingsProfile[];
  bestFp: string | null;
}) {
  const [vs, setVs] = useState<string>(DEFAULT_VS);
  const [data, setData] = useState<ProfileWhy | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErr(null);
    api
      .profileWhy(fingerprint, vs === DEFAULT_VS ? null : vs)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) {
          setData(null);
          setErr(e instanceof Error ? e.message : "Could not read the comparison");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [fingerprint, vs]);

  // Reference choices: the other profiles, best Overall first, named by call sign.
  const options = useMemo(
    () =>
      profiles
        .filter((p) => p.fingerprint !== fingerprint)
        .slice()
        .sort((x, y) => (y.overall ?? -1) - (x.overall ?? -1)),
    [profiles, fingerprint],
  );

  const gap = data?.gap.points ?? null;
  const title =
    gap == null || Math.abs(gap) < 0.05 ? "Where the gap lives" : gap > 0 ? "Why it wins" : "Why it loses";
  const refWhy: Record<string, string> = {
    sqm_off: "the unshaped link",
    crown: "the crown",
    best_other: "the best other profile",
    chosen: "your pick",
  };

  return (
    <FoldCard
      sx={{ mb: 0 }}
      title={title}
      summary={
        data ? (
          <>
            vs <strong>{data.b.name}</strong> ({refWhy[data.reference.why] ?? data.reference.why}):{" "}
            <strong>{signed(gap, 1)}</strong> Overall points
            {data.gap.clear == null ? "" : data.gap.clear ? ", clear of noise" : `, within noise (±${(data.gap.se ?? 0).toFixed(1)})`}.
            <HelpTip title="What the Overall gap is made of: per crown leg (the points add up to the gap under the weighted crown), per phase of the page load (which part moved), and per site (is the edge everywhere or on one page). Every delta carries the noise bar the crown uses to call a tie. Signed from this profile's side: positive means this profile is ahead." />
          </>
        ) : loading ? (
          "Reading the comparison…"
        ) : (
          err ?? "No comparison available yet."
        )
      }
      actions={
        <FormControl size="small" sx={{ minWidth: 200 }} onClick={(e) => e.stopPropagation()}>
          <InputLabel id="why-vs">Compare against</InputLabel>
          <Select
            labelId="why-vs"
            label="Compare against"
            value={vs}
            onChange={(e) => setVs(String(e.target.value))}
          >
            <MenuItem value={DEFAULT_VS}>Default (SQM off, else the crown)</MenuItem>
            {options.map((p) => (
              <MenuItem key={p.fingerprint} value={p.fingerprint}>
                {p.name ?? p.label}
                {p.fingerprint === bestFp ? " · crown" : ""}
                {p.overall != null ? ` · ${p.overall.toFixed(1)}` : ""}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      }
    >
      {loading && !data ? (
        <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
          <CircularProgress size={18} />
          <Typography variant="body2" color="text.secondary">
            Re-deriving the recent runs per site…
          </Typography>
        </Box>
      ) : data ? (
        <Stack spacing={2}>
          <Typography variant="body1">{data.verdict}</Typography>
          <Box>
            <Typography variant="subtitle2" gutterBottom>
              By crown leg
              {data.exact ? "" : " (not additive under this crown — each row is a one-leg swap)"}
            </Typography>
            <LegsTable legs={data.legs} bName={data.b.name} />
          </Box>
          <Box>
            <Typography variant="subtitle2" gutterBottom>
              By phase of the page load
            </Typography>
            <PhasesTable phases={data.phases} bName={data.b.name} />
          </Box>
          <Box>
            <Typography variant="subtitle2" gutterBottom>
              By site
              <Typography component="span" variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                newest {data.site_runs.a} / {data.site_runs.b} runs re-derived from raw (cap {data.site_run_limit} a side)
              </Typography>
            </Typography>
            <SitesTable sites={data.sites} legs={data.legs} />
          </Box>
          <Stack spacing={0.25}>
            {data.notes.map((n) => (
              <Typography key={n} variant="caption" color="text.secondary">
                {n}
              </Typography>
            ))}
            <Typography variant="caption" color="text.secondary">
              This profile: {data.a.overall != null ? data.a.overall.toFixed(1) : "—"} over {data.a.iterations} iterations ({data.a.runs} runs).{" "}
              {data.b.name}: {data.b.overall != null ? data.b.overall.toFixed(1) : "—"} over {data.b.iterations} iterations ({data.b.runs} runs). Methodology {data.methodology}.
            </Typography>
          </Stack>
        </Stack>
      ) : (
        <Typography variant="body2" color="text.secondary">
          {err ?? "Nothing to compare yet."}
        </Typography>
      )}
    </FoldCard>
  );
}

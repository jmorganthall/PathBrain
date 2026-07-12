import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TablePagination from "@mui/material/TablePagination";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import ArrowDownwardIcon from "@mui/icons-material/ArrowDownward";

import { api } from "../api/client";
import type {
  ApplyProfileResult,
  AxisSeriesResponse,
  DerivationAudit,
  ProfilePauseRollup,
  RunSummary,
  SettingsProfile,
} from "../api/types";
import SeriesChart from "../components/SeriesChart";
import Waterfall from "../components/Waterfall";
import StatusChip from "../components/StatusChip";
import Loading from "../components/Loading";
import EmptyState from "../components/EmptyState";
import { fmtDateTime, fmtScore } from "../utils/format";
import { profileValue } from "../utils/profileFields";
import { rankByMetric, rankColor } from "../utils/ranking";
import { useMetricMeta } from "../utils/metrics";
import { sopsColor } from "../theme";

// Headline colours mirror the Dashboard/History charts (Overall is the bright lead line).
const AXIS_COLORS: Record<string, string> = {
  overall: "#eceff1",
  responsiveness: "#ffa726",
  speed: "#4dd0e1",
  smoothness: "#ab47bc",
  stability: "#81c784",
  completion: "#90a4ae",
};
const axisColor = (key: string) => AXIS_COLORS[key] ?? "#4dd0e1";

export default function ProfileDetail() {
  const { fingerprint = "" } = useParams<{ fingerprint: string }>();
  const navigate = useNavigate();
  const [profile, setProfile] = useState<SettingsProfile | null>(null);
  const [allProfiles, setAllProfiles] = useState<SettingsProfile[]>([]);
  // The current methodology's crown metrics (from the profiles response's `overall_metrics`), so
  // the standings boxes always follow the crown — never a hardcoded axis set.
  const [overallMetrics, setOverallMetrics] = useState<string[]>([]);
  const [currentFp, setCurrentFp] = useState<string | null>(null);
  const [bestFp, setBestFp] = useState<string | null>(null);
  const [series, setSeries] = useState<AxisSeriesResponse | null>(null);
  const [pauses, setPauses] = useState<ProfilePauseRollup | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Read-only data-integrity audit (re-derive oldest+newest runs from raw, diff vs stored).
  const [audit, setAudit] = useState<DerivationAudit | null>(null);
  const [auditing, setAuditing] = useState(false);
  const [auditErr, setAuditErr] = useState<string | null>(null);
  const runAudit = useCallback(async () => {
    setAuditing(true);
    setAuditErr(null);
    setAudit(null);
    try {
      setAudit(await api.verifyProfileDerivation(fingerprint));
    } catch (e) {
      setAuditErr(e instanceof Error ? e.message : "Audit failed");
    } finally {
      setAuditing(false);
    }
  }, [fingerprint]);

  // Apply (preview → confirm → commit) state.
  const [applyPreview, setApplyPreview] = useState<ApplyProfileResult | null>(null);
  const [applying, setApplying] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  const loadPage = useCallback(
    async (p: number, rpp: number) => {
      const rows = await api.history(rpp, p * rpp, fingerprint);
      setRuns(rows);
    },
    [fingerprint],
  );

  useEffect(() => {
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const [profsResp, s, c] = await Promise.all([
          api.settingsProfiles(false),
          api.axisSeries(200, fingerprint),
          api.historyCount(fingerprint),
        ]);
        setProfile(profsResp.profiles.find((p) => p.fingerprint === fingerprint) ?? null);
        setAllProfiles(profsResp.profiles);
        setOverallMetrics(profsResp.overall_metrics ?? []);
        setCurrentFp(profsResp.current_fingerprint);
        setBestFp(profsResp.best_fingerprint);
        setSeries(s);
        setTotal(c.count);
        await loadPage(0, rowsPerPage);
        setPage(0);
        // Best-effort pause roll-up (reads raw across runs, so don't block the page on it).
        api.profilePauses(fingerprint).then(setPauses).catch(() => setPauses(null));
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load profile");
      } finally {
        setLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fingerprint]);

  const handlePage = (_e: unknown, next: number) => {
    setPage(next);
    loadPage(next, rowsPerPage).catch(() => {});
  };
  const handleRowsPerPage = (e: React.ChangeEvent<HTMLInputElement>) => {
    const rpp = parseInt(e.target.value, 10);
    setRowsPerPage(rpp);
    setPage(0);
    loadPage(0, rpp).catch(() => {});
  };

  const previewApply = async () => {
    try {
      setApplyPreview(await api.applyProfile(fingerprint, true));
    } catch (e) {
      setToast(e instanceof Error ? e.message : "Preview failed");
    }
  };
  const commitApply = async () => {
    setApplying(true);
    try {
      const r = await api.applyProfile(fingerprint, false, false);
      setApplyPreview(null);
      setToast(r.already_applied ? "Profile already active." : "Profile applied to the firewall.");
      setCurrentFp(fingerprint);
    } catch (e) {
      setToast(e instanceof Error ? e.message : "Apply failed");
    } finally {
      setApplying(false);
    }
  };

  // This profile's standing (1 = best) among all profiles, for the Overall + each CROWN metric
  // the current methodology corners over (from the profiles response's `overall_metrics`) — the
  // same crown-driven ranking as the Settings-Impact table, never a hardcoded axis set. Each crown
  // metric ranks by its field-normalized-raw value via the `crown:<metric>` key (→ `crown_norm`).
  const metricMeta = useMetricMeta();
  const rankedMetrics = useMemo(
    () => [
      { key: "overall", label: "Overall" },
      ...overallMetrics.map((k) => ({ key: `crown:${k}`, label: metricMeta(k).label })),
    ],
    [overallMetrics, metricMeta],
  );
  const standings = useMemo(
    () =>
      rankedMetrics.map((m) => {
        const rk = rankByMetric(allProfiles, m.key);
        const rank = profile ? rk.rankByFp[profile.fingerprint] ?? null : null;
        const raw = profile ? profileValue(profile, m.key) : null;
        // How far behind the crown (rank 1) this profile is, as a percentage. All these values are
        // higher-is-better (Overall 0–100, crown percentiles), so the best is the field max.
        const values = allProfiles
          .map((p) => profileValue(p, m.key))
          .filter((v): v is number => v != null);
        const best = values.length ? Math.max(...values) : null;
        const pctWorse =
          rank != null && rank > 1 && best != null && best > 0 && raw != null
            ? ((best - raw) / best) * 100
            : null;
        return { ...m, rank, total: rk.total, raw, pctWorse };
      }),
    [rankedMetrics, allProfiles, profile],
  );

  if (loading) return <Loading label="Loading profile…" />;

  const isActive = currentFp != null && currentFp === fingerprint;
  const isBest = bestFp != null && bestFp === fingerprint;
  const headlineLines = (series?.axes ?? [])
    .filter((a) => a.role === "headline")
    .map((a) => ({ key: a.key, name: a.label, color: axisColor(a.key) }));

  return (
    <Box>
      <Button startIcon={<ArrowBackIcon />} onClick={() => navigate("/settings")} sx={{ mb: 2 }}>
        Back to Settings Impact
      </Button>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}
      {toast && (
        <Alert severity="info" sx={{ mb: 2 }} onClose={() => setToast(null)}>
          {toast}
        </Alert>
      )}

      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        spacing={1}
        sx={{ mb: 2 }}
      >
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="h4" sx={{ wordBreak: "break-word" }}>
            {profile?.label ?? "Profile"}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {fingerprint}
          </Typography>
        </Box>
        <Tooltip title="Write this profile's shaper settings to the firewall now. You'll preview the exact changes and confirm first.">
          <span>
            <Button
              variant="contained"
              onClick={previewApply}
              disabled={applying || isActive}
            >
              {isActive ? "Active" : "Apply this profile"}
            </Button>
          </span>
        </Tooltip>
      </Stack>

      {/* Status chips */}
      <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 2 }}>
        {isActive && <Chip color="info" label="active on firewall" />}
        {isBest && <Chip color="success" label="best (crown)" />}
        {profile && !profile.confident && (
          <Chip color="warning" variant="outlined" label="limited data" />
        )}
        {profile && (
          <Chip variant="outlined" label={`${profile.iterations} iterations · ${profile.count} runs`} />
        )}
      </Stack>

      {/* Standings: this profile's rank (1 = best) per Overall + headline axis, green→red. */}
      {standings.length > 0 && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="h6" gutterBottom>
              Standings
            </Typography>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
              Where this profile ranks among all {standings[0].total || 0} measured profiles (1 = best),
              for the Overall and each <b>crown metric</b> the current methodology corners over. When
              it isn&apos;t #1, the red arrow shows how far behind the crown (#1) it is, as a percent.
            </Typography>
            <Box
              sx={{
                display: "grid",
                gap: 1.5,
                gridTemplateColumns: { xs: "repeat(2, 1fr)", sm: `repeat(${standings.length}, 1fr)` },
              }}
            >
              {standings.map((s) => (
                <Box
                  key={s.key}
                  sx={{ p: 1.5, borderRadius: 1, border: 1, borderColor: "divider", textAlign: "center" }}
                >
                  <Typography variant="overline" color="text.secondary" sx={{ display: "block" }}>
                    {s.label}
                  </Typography>
                  <Typography
                    sx={{ fontWeight: 800, fontSize: "1.6rem", lineHeight: 1.1, color: rankColor(s.rank, s.total) }}
                  >
                    {s.rank == null ? "—" : `#${s.rank}`}
                  </Typography>
                  {s.pctWorse != null && (
                    <Box
                      sx={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        gap: 0.25,
                        color: "error.main",
                      }}
                    >
                      <ArrowDownwardIcon sx={{ fontSize: "0.95rem" }} />
                      <Typography component="span" variant="caption" sx={{ fontWeight: 700 }}>
                        {s.pctWorse < 0.1 ? "<0.1" : s.pctWorse.toFixed(1)}% vs crown
                      </Typography>
                    </Box>
                  )}
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                    {s.rank == null ? "no score" : `of ${s.total} · score ${s.raw}`}
                  </Typography>
                </Box>
              ))}
            </Box>
          </CardContent>
        </Card>
      )}

      {/* Data-integrity audit: prove old and new runs are like-for-like by re-deriving each from
          its immutable raw and diffing against the stored value. */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
            <Typography variant="h6" sx={{ flexGrow: 1 }}>
              Data integrity
            </Typography>
            <Tooltip title="Re-derive this profile's oldest & newest runs from their immutable raw and check the stored metrics still reproduce. Read-only — changes nothing.">
              <span>
                <Button size="small" variant="outlined" onClick={runAudit} disabled={auditing}>
                  {auditing ? "Verifying…" : "Verify old vs new"}
                </Button>
              </span>
            </Tooltip>
          </Stack>
          <Typography variant="body2" color="text.secondary" sx={{ mb: audit || auditErr ? 1.5 : 0 }}>
            Checks whether a metric means the same thing across time — that stored values reproduce
            exactly from raw under the current derivation. If old runs drift while new ones don&apos;t,
            history was computed under a formula that has since changed and needs a re-derive.
          </Typography>
          {auditErr && <Alert severity="error">{auditErr}</Alert>}
          {audit && (
            <Box>
              <Alert severity={audit.consistent ? "success" : audit.stale_history ? "warning" : "error"} sx={{ mb: 1 }}>
                {audit.consistent
                  ? `Like-for-like: all sampled runs reproduce exactly from raw (derivation ${audit.current_derivation}).`
                  : audit.stale_history
                    ? "Stale history: older runs were computed under a formula that has since changed and were never re-derived. Run Re-derive (Methodology page) to bring them onto the current derivation."
                    : "Drift detected: some runs don't reproduce from raw under the current derivation."}
              </Alert>
              <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
                {([["Oldest runs", audit.oldest], ["Newest runs", audit.newest]] as const).map(
                  ([label, c]) => (
                    <Box key={label} sx={{ flex: 1 }}>
                      <Typography variant="subtitle2">{label}</Typography>
                      <Typography variant="body2" color={c.consistent ? "success.main" : "warning.main"}>
                        {c.checked - c.drifting}/{c.checked} reproduce from raw
                        {c.drifting > 0 && ` · drift: ${c.drift_metrics.join(", ")}`}
                      </Typography>
                    </Box>
                  ),
                )}
              </Stack>
              <Typography variant="caption" color="text.disabled" sx={{ display: "block", mt: 1 }}>
                {audit.total_runs} total run{audit.total_runs === 1 ? "" : "s"} · sampled oldest &amp; newest
              </Typography>
            </Box>
          )}
        </CardContent>
      </Card>

      <Box sx={{ display: "grid", gap: 2 }}>
        {profile?.metrics && profile.metrics["nav_response"] != null && (
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                Load waterfall (median)
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 2 }}>
                This profile&apos;s median page load, split into independent phases. Setup up to{" "}
                <b>first byte</b> (DNS/TCP/TLS/TTFB) is weather-dominated, not shaping. Judge this
                profile on the amber <b>Delivery</b> phase (first byte → response done) — body
                delivery through the queue, the one phase your shaper moves. <b>Client render</b> is
                shaping-immune client CPU and should match across profiles.
              </Typography>
              <Waterfall metrics={profile.metrics} />
            </CardContent>
          </Card>
        )}

        {pauses && pauses.urls.length > 0 && (
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                Where&apos;s the pause? (median across {pauses.runs} run{pauses.runs === 1 ? "" : "s"})
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 2 }}>
                The longest <b>void</b> in each page load — where nothing finished — rolled up across
                this profile&apos;s runs: typical duration, where it falls, and whether it&apos;s{" "}
                <b>network</b> (byte-delivery, the part your shaper moves) or <b>render</b> (main
                thread, shaping-immune). This is what the crown&apos;s network-stall leg is built on.
              </Typography>
              <Stack spacing={1}>
                {pauses.urls.map((d) => {
                  const phaseLabel: Record<string, string> = {
                    pre_fcp: "before first paint",
                    fcp_lcp: "first paint → main content",
                    lcp_load: "post-LCP settle",
                    post_load: "after load",
                  };
                  const netPct = d.network_fraction != null ? Math.round(d.network_fraction * 100) : null;
                  const attrColor =
                    d.attribution === "render" ? "warning" : d.attribution === "network" ? "info" : "default";
                  return (
                    <Box
                      key={d.url}
                      sx={{
                        display: "flex",
                        alignItems: "center",
                        flexWrap: "wrap",
                        gap: 1,
                        p: 1,
                        borderRadius: 1,
                        border: 1,
                        borderColor: "divider",
                      }}
                    >
                      <Typography variant="body2" sx={{ minWidth: 0, flex: 1 }} noWrap title={d.url}>
                        {d.url}
                      </Typography>
                      <Typography variant="body2" sx={{ fontWeight: 700 }}>
                        {Math.round(d.median_void_ms)}ms void
                      </Typography>
                      <Chip size="small" variant="outlined" label={phaseLabel[d.phase] ?? d.phase} />
                      <Chip
                        size="small"
                        color={attrColor as "warning" | "info" | "default"}
                        variant={d.attribution === "render" || d.attribution === "network" ? "filled" : "outlined"}
                        label={
                          netPct != null && d.attribution
                            ? `${d.attribution} · ${netPct}% network`
                            : d.attribution ?? "unknown"
                        }
                      />
                      <Typography variant="caption" color="text.secondary">
                        {d.runs} run{d.runs === 1 ? "" : "s"}
                      </Typography>
                    </Box>
                  );
                })}
              </Stack>
            </CardContent>
          </Card>
        )}

        <Card>
          <CardContent>
            <Typography variant="h6" gutterBottom>
              Scores over time (this profile)
            </Typography>
            {series && series.points.length > 0 && headlineLines.length > 0 ? (
              <SeriesChart data={series.points} yDomain={[0, 100]} lines={headlineLines} />
            ) : (
              <Typography variant="body2" color="text.secondary">
                No comparable scored runs for this profile yet.
              </Typography>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent>
            <Typography variant="h6" sx={{ mb: 1 }}>
              Run history ({total})
            </Typography>
            {total === 0 ? (
              <EmptyState
                title="No runs for this profile"
                description="Runs captured while this firewall profile was live will appear here."
              />
            ) : (
              <>
                <TableContainer>
                  <Table size="small">
                    <TableHead>
                      <TableRow>
                        <TableCell>ID</TableCell>
                        <TableCell>Time</TableCell>
                        <TableCell>Label</TableCell>
                        <TableCell>Status</TableCell>
                        <TableCell align="right">Overall</TableCell>
                        <TableCell align="right">Respons.</TableCell>
                        <TableCell align="right">Smoothness</TableCell>
                        <TableCell align="right">Speed</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {runs.map((r) => (
                        <TableRow
                          key={r.id}
                          hover
                          sx={{ cursor: "pointer", opacity: r.legacy ? 0.6 : 1 }}
                          onClick={() => navigate(`/runs/${r.id}`)}
                        >
                          <TableCell>#{r.id}</TableCell>
                          <TableCell>{fmtDateTime(r.created_at)}</TableCell>
                          <TableCell>{r.label ?? "—"}</TableCell>
                          <TableCell>
                            <StatusChip status={r.status} />
                          </TableCell>
                          {r.legacy ? (
                            <TableCell align="right" colSpan={4}>
                              <Tooltip title="Not comparable under the current methodology.">
                                <Chip size="small" variant="outlined" label="legacy" />
                              </Tooltip>
                            </TableCell>
                          ) : (
                            <>
                              <TableCell align="right">
                                <Typography component="span" sx={{ fontWeight: 700, color: sopsColor(r.overall) }}>
                                  {fmtScore(r.overall)}
                                </Typography>
                              </TableCell>
                              <TableCell align="right">{fmtScore(r.responsiveness)}</TableCell>
                              <TableCell align="right">{fmtScore(r.smoothness)}</TableCell>
                              <TableCell align="right">{fmtScore(r.speed)}</TableCell>
                            </>
                          )}
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableContainer>
                <TablePagination
                  component="div"
                  count={total}
                  page={page}
                  onPageChange={handlePage}
                  rowsPerPage={rowsPerPage}
                  onRowsPerPageChange={handleRowsPerPage}
                  rowsPerPageOptions={[10, 25, 50, 100]}
                />
              </>
            )}
          </CardContent>
        </Card>
      </Box>

      {/* Apply confirmation dialog (preview of exact field writes). */}
      <Dialog open={applyPreview != null} onClose={() => setApplyPreview(null)} maxWidth="sm" fullWidth>
        <DialogTitle>Apply “{profile?.label ?? fingerprint}”?</DialogTitle>
        <DialogContent dividers>
          {applyPreview?.already_applied ? (
            <Typography variant="body2">This profile is already live on the firewall.</Typography>
          ) : applyPreview && (applyPreview.changes?.length ?? 0) > 0 ? (
            <Table size="small">
              <TableBody>
                {applyPreview.changes!.map((c, i) => (
                  <TableRow key={i}>
                    <TableCell sx={{ border: 0 }}>{c.field_label}</TableCell>
                    <TableCell align="right" sx={{ border: 0 }}>
                      <Typography component="span" variant="caption" color="text.secondary">
                        {String(c.from ?? "—")} →{" "}
                      </Typography>
                      <Typography component="span" sx={{ fontWeight: 600 }}>
                        {String(c.to ?? "—")}
                      </Typography>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <Typography variant="body2" color="text.secondary">
              No field changes needed.
            </Typography>
          )}
          {(applyPreview?.warnings?.length ?? 0) > 0 && (
            <Alert severity="warning" sx={{ mt: 2 }}>
              {applyPreview!.warnings.join(" ")}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setApplyPreview(null)}>Cancel</Button>
          <Button variant="contained" onClick={commitApply} disabled={applying}>
            {applying ? "Applying…" : "Apply"}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import LinearProgress from "@mui/material/LinearProgress";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import RefreshIcon from "@mui/icons-material/Refresh";

import { api } from "../api/client";
import type { WeatherSensitivity } from "../api/types";
import { FoldCard, HelpTip } from "../components/Explain";
import PageHeader from "../components/PageHeader";
import { Link as RouterLink } from "react-router-dom";

// The Weather tab — the measured-conditions view, spun off the Settings-Impact card once
// it grew past a card's altitude. Everything here is strictly informational (no scores
// change): weather is defined by each run's own clean covariate readings — probe
// DNS/TCP/TLS/latency plus the load's connection-setup phases, the signals the shaper
// can't move — never by the clock.
//
// The page auto-loads: the endpoint scans every comparable run, but it is memoized
// server-side on the field stamp, so only the first visit after new data pays the pass.
export default function Weather() {
  const [data, setData] = useState<WeatherSensitivity | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.weatherSensitivity());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the weather analysis.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // The verdict reads only CLEAN covariates (profile-orthogonal): a shaped covariate
  // correlating with the crown is the shaper working, not weather.
  const cleanSensitive = (data?.rows ?? []).filter((r) => r.clean && r.weather_sensitive);
  const strongest = cleanSensitive[0]; // server sorts by |within-profile ρ| desc
  const verdict = !data
    ? null
    : cleanSensitive.length > 0
      ? `Network conditions noticeably move the scores: ${cleanSensitive.length} condition→score link${cleanSensitive.length === 1 ? "" : "s"} ` +
        `above the ${data.trend_rho} bar. Strongest: ${strongest.covariate_label} → ${strongest.metric_label} ` +
        `(within-profile correlation ${strongest.within_profile_spearman ?? strongest.pooled_spearman}). ` +
        "The “vs weather” reading on the standings carries real signal."
      : `The scores look robust to network conditions: no condition reaches the ${data.trend_rho} correlation bar ` +
        `within a profile, over ${data.runs_analyzed} runs and ${data.profiles_analyzed} profiles. ` +
        "A weather adjustment would change little here.";

  return (
    <Box>
      <PageHeader
        title="Weather"
        lead="How network conditions outside the shaper's control — DNS, connection and handshake times — affect the scores."
        more={
          <>
            Conditions are measured on every run, never inferred from the clock. The per-profile
            readings (vs weather, severity, weather-beater) stay on the{" "}
            <RouterLink to="/settings" style={{ color: "inherit" }}>Profile standings</RouterLink> page next to the
            standings they qualify; this page is the analysis behind them.
          </>
        }
        compact
        actions={
          <Button size="small" startIcon={<RefreshIcon />} onClick={() => void load()} disabled={loading}>
            Refresh
          </Button>
        }
      />

      {loading && (
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2 }}>
          <CircularProgress size={20} />
          <Typography variant="body2" color="text.secondary">
            Analyzing every comparable run… (cached until new data arrives)
          </Typography>
        </Stack>
      )}
      {error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}

      {data && (
        <>
          <Alert severity={cleanSensitive.length > 0 ? "warning" : "success"} sx={{ mb: 2 }}>
            {verdict}
          </Alert>

          {/* The budget question that decides architecture: of the run-to-run noise a
              profile shows, how much could the clean covariates jointly account for AT
              ALL? That share is the ceiling on any covariate-based weather adjustment —
              and its complement is the measured justification for the duel's paired
              design, which controls for the weather no probe sees. */}
          {data.variance && (
            <Card sx={{ mb: 2 }}>
              <CardContent>
                <Typography variant="h6">How much score noise comes from network conditions</Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                  The most a weather adjustment could ever remove — a ceiling, not a promise.
                  <HelpTip title="Adjusted within-profile R² of the clean covariates against each crown metric and the Overall. The profile is held fixed, so its identity can't masquerade as weather. Linear and in-sample." />
                </Typography>
                <Alert severity="info" icon={false} sx={{ mb: 1.5, py: 0.5 }}>
                  <Typography variant="caption">{data.variance.headline}</Typography>
                </Alert>
                <Stack spacing={0.75}>
                  {data.variance.outcomes.map((o) => (
                    <Box key={o.outcome}>
                      <Stack direction="row" spacing={1} alignItems="baseline" flexWrap="wrap" useFlexGap>
                        <Typography variant="body2" sx={{ fontWeight: 600, minWidth: 120 }}>
                          {o.outcome_label}
                        </Typography>
                        {o.explained_share != null ? (
                          <Typography variant="caption" color="text.secondary">
                            <b>{Math.round(o.explained_share * 100)}%</b> of the run-to-run noise
                            {o.within_sd != null && o.residual_sd != null && (
                              <>
                                {" "}· noise ±{o.within_sd} → ±{o.residual_sd} {o.unit} if adjusted
                              </>
                            )}
                            {" "}· {o.runs.toLocaleString()} runs over {o.profiles} profiles
                          </Typography>
                        ) : (
                          <Typography variant="caption" color="text.secondary">
                            {o.why ?? "not enough data yet"}
                          </Typography>
                        )}
                      </Stack>
                      {o.explained_share != null && (
                        <Tooltip
                          title={
                            "Adjusted R² of the clean covariates against this outcome, " +
                            "within-profile (the profile held fixed, so its identity can't " +
                            "masquerade as weather). Linear and in-sample — read it as the " +
                            "ceiling on what a weather adjustment could remove, not a promise."
                          }
                        >
                          <LinearProgress
                            variant="determinate"
                            value={Math.min(100, o.explained_share * 100)}
                            sx={{ height: 5, borderRadius: 2, mt: 0.25 }}
                          />
                        </Tooltip>
                      )}
                    </Box>
                  ))}
                </Stack>
              </CardContent>
            </Card>
          )}

          <FoldCard
            title="Which conditions affect which scores"
            summary={
              <>
                {data.rows.length} condition→score pairs, strongest first.
                <HelpTip title="The same-profile correlation is the causal signal: the profile is held fixed, so only conditions vary. The all-runs correlation mixes in differences between profiles and is context only." />
              </>
            }
          >
              <TableContainer sx={{ maxHeight: 560, overflowX: "auto" }}>
                <Table size="small" stickyHeader>
                  <TableHead>
                    <TableRow>
                      <TableCell>Condition</TableCell>
                      <TableCell>Score it moves</TableCell>
                      <TableCell align="right">
                        <Tooltip title="Spearman ρ computed within each profile (profile held fixed), median across profiles — the causal 'does weather move this metric' signal.">
                          <span>Effect (same profile)</span>
                        </Tooltip>
                      </TableCell>
                      <TableCell align="right">
                        <Tooltip title="Spearman ρ over all runs pooled — mixes the weather effect with between-profile differences; context only.">
                          <span>Effect (all runs)</span>
                        </Tooltip>
                      </TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {data.rows.map((r) => (
                      <TableRow key={`${r.covariate}-${r.metric}`}>
                        <TableCell>
                          <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap">
                            <span>{r.covariate_label}</span>
                            {r.clean ? (
                              <Tooltip title="Profile-orthogonal — safe to use as a weather signal.">
                                <Chip size="small" variant="outlined" color="success" label="clean" />
                              </Tooltip>
                            ) : (
                              <Tooltip title="The shaper itself moves this — transparency only; must never adjust with it.">
                                <Chip size="small" variant="outlined" color="warning" label="shaped" />
                              </Tooltip>
                            )}
                          </Stack>
                        </TableCell>
                        <TableCell>{r.metric_label}</TableCell>
                        <TableCell align="right">
                          <Typography
                            variant="body2"
                            component="span"
                            sx={{ fontWeight: r.clean && r.weather_sensitive ? 700 : 400 }}
                            color={r.clean && r.weather_sensitive ? "warning.main" : "inherit"}
                          >
                            {r.within_profile_spearman != null
                              ? r.within_profile_spearman.toFixed(2)
                              : "—"}
                          </Typography>
                          <Typography variant="caption" color="text.secondary">
                            {" "}({r.within_profile_profiles}p)
                          </Typography>
                        </TableCell>
                        <TableCell align="right">
                          {r.pooled_spearman != null ? r.pooled_spearman.toFixed(2) : "—"}
                          <Typography variant="caption" color="text.secondary">
                            {" "}({r.pooled_n})
                          </Typography>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
                {data.runs_analyzed} runs across {data.profiles_analyzed} profiles. A correlation of{" "}
                {data.trend_rho} or more counts as weather-sensitive; a profile needs {data.within_profile_min_points}+ runs to contribute.
              </Typography>
          </FoldCard>
        </>
      )}
    </Box>
  );
}

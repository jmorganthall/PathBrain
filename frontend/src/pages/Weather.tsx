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
      ? `Weather moves the crown: ${cleanSensitive.length} clean covariate↔metric pair(s) over ` +
        `|ρ| ${data.trend_rho} — strongest: ${strongest.covariate_label} → ${strongest.metric_label} ` +
        `(within-profile ρ ${strongest.within_profile_spearman ?? strongest.pooled_spearman}). ` +
        "A vs-weather reading would carry real signal."
      : `Crown looks weather-robust: no clean covariate reaches |ρ| ${data.trend_rho} within-profile ` +
        `across ${data.runs_analyzed} runs / ${data.profiles_analyzed} profiles. ` +
        "A weather adjustment would do little here.";

  return (
    <Box>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        spacing={1}
        sx={{ mb: 2 }}
      >
        <Box>
          <Typography variant="h4" sx={{ fontWeight: 700 }}>
            Weather
          </Typography>
          <Typography variant="body2" color="text.secondary">
            What ambient network conditions do to the numbers we crown on.
            <HelpTip title="Conditions are measured on every run, never inferred from the clock. Per-profile readings (vs weather, severity, weather-beater flags) stay on Settings Impact next to the standings they qualify." />
          </Typography>
        </Box>
        <Button size="small" startIcon={<RefreshIcon />} onClick={() => void load()} disabled={loading}>
          Refresh
        </Button>
      </Stack>

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
                <Typography variant="h6">How much of the noise is measurable weather?</Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                  The most a weather adjustment could ever remove. A ceiling, not a promise.
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
                            <b>{Math.round(o.explained_share * 100)}%</b> explainable
                            {o.within_sd != null && o.residual_sd != null && (
                              <>
                                {" "}· ±{o.within_sd} → ±{o.residual_sd} {o.unit}
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
            title="Weather sensitivity"
            summary={
              <>
                Which covariate moves which crown metric, {data.rows.length} pairs.
                <HelpTip title="Within-profile ρ is the causal signal. Pooled ρ mixes in between-profile differences and is context only." />
              </>
            }
          >
              <TableContainer sx={{ maxHeight: 560, overflowX: "auto" }}>
                <Table size="small" stickyHeader>
                  <TableHead>
                    <TableRow>
                      <TableCell>Weather covariate</TableCell>
                      <TableCell>Crown metric</TableCell>
                      <TableCell align="right">
                        <Tooltip title="Spearman ρ computed within each profile (profile held fixed), median across profiles — the causal 'does weather move this metric' signal.">
                          <span>Within-profile ρ</span>
                        </Tooltip>
                      </TableCell>
                      <TableCell align="right">
                        <Tooltip title="Spearman ρ over all runs pooled — mixes the weather effect with between-profile differences; context only.">
                          <span>Pooled ρ</span>
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
                {data.runs_analyzed} runs across {data.profiles_analyzed} profiles · pairs ranked by
                |within-profile ρ| · |ρ| ≥ {data.trend_rho} counts as weather-sensitive · a profile
                needs ≥ {data.within_profile_min_points} runs to contribute a within-profile ρ.
              </Typography>
          </FoldCard>
        </>
      )}
    </Box>
  );
}

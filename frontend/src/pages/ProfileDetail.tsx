import { useCallback, useEffect, useState } from "react";
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

import { api } from "../api/client";
import type {
  ApplyProfileResult,
  AxisSeriesResponse,
  RunSummary,
  SettingsProfile,
} from "../api/types";
import SeriesChart from "../components/SeriesChart";
import StatusChip from "../components/StatusChip";
import Loading from "../components/Loading";
import EmptyState from "../components/EmptyState";
import { fmtDateTime, fmtScore } from "../utils/format";
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
  const [currentFp, setCurrentFp] = useState<string | null>(null);
  const [bestFp, setBestFp] = useState<string | null>(null);
  const [series, setSeries] = useState<AxisSeriesResponse | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
        setCurrentFp(profsResp.current_fingerprint);
        setBestFp(profsResp.best_fingerprint);
        setSeries(s);
        setTotal(c.count);
        await loadPage(0, rowsPerPage);
        setPage(0);
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

      {/* Summary chips */}
      <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 2 }}>
        {isActive && <Chip color="info" label="active on firewall" />}
        {isBest && <Chip color="success" label="best (crown)" />}
        {profile && !profile.confident && (
          <Chip color="warning" variant="outlined" label="limited data" />
        )}
        {profile?.overall != null && (
          <Chip
            label={`Overall ${profile.overall}`}
            sx={{ fontWeight: 700, color: sopsColor(profile.overall) }}
            variant="outlined"
          />
        )}
        {profile?.scores?.responsiveness != null && (
          <Chip variant="outlined" label={`Respons. ${profile.scores.responsiveness}`} />
        )}
        {profile?.median != null && <Chip variant="outlined" label={`Smoothness ${profile.median}`} />}
        {profile?.speed?.median != null && (
          <Chip variant="outlined" label={`Speed ${profile.speed.median}`} />
        )}
        {profile && (
          <Chip variant="outlined" label={`${profile.iterations} iterations · ${profile.count} runs`} />
        )}
      </Stack>

      <Box sx={{ display: "grid", gap: 2 }}>
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

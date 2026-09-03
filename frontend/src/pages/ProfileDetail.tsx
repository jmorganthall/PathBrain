import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import ButtonGroup from "@mui/material/ButtonGroup";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
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
import useMediaQuery from "@mui/material/useMediaQuery";
import { useTheme } from "@mui/material/styles";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import ArrowDownwardIcon from "@mui/icons-material/ArrowDownward";
import ArrowDropDownIcon from "@mui/icons-material/ArrowDropDown";
import BoltIcon from "@mui/icons-material/Bolt";
import EditIcon from "@mui/icons-material/Edit";
import SaveIcon from "@mui/icons-material/Save";
import IconButton from "@mui/material/IconButton";
import FormControlLabel from "@mui/material/FormControlLabel";
import Switch from "@mui/material/Switch";
import Link from "@mui/material/Link";
import TextField from "@mui/material/TextField";

import { api } from "../api/client";
import type {
  ApplyProfileResult,
  AxisSeriesResponse,
  CrownConfidence,
  DerivationAudit,
  DuelProfileLedger,
  ProfilePauseRollup,
  ProfileTest,
  RunSummary,
  SettingsProfile,
} from "../api/types";
import SeriesChart from "../components/SeriesChart";
import Waterfall from "../components/Waterfall";
import StatusChip from "../components/StatusChip";
import Loading from "../components/Loading";
import EmptyState from "../components/EmptyState";
import { FoldCard, HelpTip } from "../components/Explain";
import { fmtDateTime, fmtScore, fmtTimeShort } from "../utils/format";
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

// The short test: one runner chunk (`runner.CHUNK_ITERATIONS`), the same length Explore's
// "Test now" runs. Sent explicitly with the request, so the label and what runs can't drift.
const QUICK_ITERATIONS = 5;

/** A duel result from this profile's side, in the ring's own vocabulary. */
const BOUT_COLOR = { win: "success", loss: "error", draw: "default" } as const;

export default function ProfileDetail() {
  const { fingerprint = "" } = useParams<{ fingerprint: string }>();
  const navigate = useNavigate();
  // A seven-column table on a 390px screen shows three columns and hides the result, the
  // scoreline and the margin — everything a bout actually says — behind a sideways scroll
  // nobody discovers. On a phone the same rows are rendered as a stacked list instead.
  const narrow = useMediaQuery(useTheme().breakpoints.down("sm"));
  const [profile, setProfile] = useState<SettingsProfile | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [allProfiles, setAllProfiles] = useState<SettingsProfile[]>([]);
  // The current methodology's crown metrics (from the profiles response's `overall_metrics`), so
  // the standings boxes always follow the crown — never a hardcoded axis set.
  const [overallMetrics, setOverallMetrics] = useState<string[]>([]);
  const [currentFp, setCurrentFp] = useState<string | null>(null);
  // The confidence bar (total iterations), so the page can say how far a top-up would go.
  const [minIterations, setMinIterations] = useState<number | null>(null);
  const [bestFp, setBestFp] = useState<string | null>(null);
  // Fingerprints statistically tied with the crown (within run-to-run noise). Lets the standings
  // say "tied for #1" instead of implying a precise, decisive rank when the top is a photo finish.
  const [coLeaders, setCoLeaders] = useState<string[]>([]);
  // The measured crown-lead-vs-noise (gap to runner-up vs the significance threshold).
  const [crownConf, setCrownConf] = useState<CrownConfidence | null>(null);
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

  // This profile's record in the ring (GET /duel/profile/{fp}). Best-effort and fetched
  // beside the page rather than blocking it: the ladder is a second opinion, not a
  // prerequisite for reading what the profile measured.
  const [ring, setRing] = useState<DuelProfileLedger | null>(null);
  // Most pairings on a continuous ladder end undecided, so the decided ones lead and the
  // rest are behind a toggle — a list where four in five rows say "0-0-2" is a list nobody
  // reads to the end.
  const [showUndecided, setShowUndecided] = useState(false);
  const [opponentsShown, setOpponentsShown] = useState(40);
  const shownOpponents = useMemo(
    () => (ring?.opponents ?? []).filter((o) => showUndecided || o.decisive),
    [ring, showUndecided]
  );
  const visibleOpponents = useMemo(
    () => shownOpponents.slice(0, opponentsShown),
    [shownOpponents, opponentsShown]
  );
  const [boutsShown, setBoutsShown] = useState(8);

  // "Test this profile": apply it, benchmark it, restore the previous settings.
  const [testMenu, setTestMenu] = useState<HTMLElement | null>(null);
  const [starting, setStarting] = useState(false);
  const [activeTest, setActiveTest] = useState<ProfileTest | null>(null);

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

  const load = useCallback(
    async ({ spinner = true }: { spinner?: boolean } = {}) => {
      if (spinner) setLoading(true);
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
        setMinIterations(profsResp.min_iterations ?? null);
        setBestFp(profsResp.best_fingerprint);
        setCoLeaders(profsResp.co_leaders ?? []);
        setCrownConf(profsResp.crown_confidence ?? null);
        setSeries(s);
        setTotal(c.count);
        await loadPage(0, rowsPerPage);
        setPage(0);
        // Best-effort side loads (each reads more than the page needs to paint, so neither
        // blocks it and neither failing empties the page).
        api.profilePauses(fingerprint).then(setPauses).catch(() => setPauses(null));
        api.duelProfile(fingerprint).then(setRing).catch(() => setRing(null));
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load profile");
      } finally {
        if (spinner) setLoading(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [fingerprint],
  );

  useEffect(() => {
    void load();
  }, [load]);

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

  // ── Test this profile ────────────────────────────────────────────────────────
  //
  // Two lengths, because there are two questions, and the page knows which one applies:
  // a profile short of the confidence bar wants topping up (`iterations` omitted), and one
  // already past it wants re-measuring — "is it still this good?" — which is an explicit
  // count. Both apply the profile, benchmark it, and restore the previous settings.
  const startTest = useCallback(
    async (iterations?: number) => {
      setTestMenu(null);
      setStarting(true);
      setError(null);
      try {
        const r = await api.testProfile(fingerprint, iterations);
        setToast(
          r.mode === "exact"
            ? `Testing this profile: ${r.iterations} iteration(s), then your settings are restored.`
            : `Topping up: ${r.iterations} iteration(s) to reach the ${r.min_iterations}-iteration minimum.`,
        );
        // Show the live stage readout straight away; the poller below keeps it fresh.
        setActiveTest((await api.profileTestCurrent()).test);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to start the profile test");
      } finally {
        setStarting(false);
      }
    },
    [fingerprint],
  );

  // Follow the test to the end, then refresh the page in place — the point of running it is
  // the new data, and a page still showing the pre-test numbers hides exactly that.
  useEffect(() => {
    if (!activeTest || (activeTest.status !== "running" && activeTest.status !== "pending")) return;
    const t = setInterval(async () => {
      try {
        const cur = (await api.profileTestCurrent()).test;
        setActiveTest(cur);
        if (cur && (cur.status === "complete" || cur.status === "failed")) {
          if (cur.status === "failed") setError(`Profile test failed: ${cur.error ?? "unknown error"}`);
          else setToast("Profile test finished — your settings were restored.");
          await load({ spinner: false });
        }
      } catch {
        /* transient; the next tick retries */
      }
    }, 3000);
    return () => clearInterval(t);
  }, [activeTest, load]);

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

  // Rename: profiles get an auto-assigned call sign, but a name you chose yourself
  // ("Living Room Fix") beats a generated one for the profile you actually care about.
  const saveName = async () => {
    const next = nameDraft.trim();
    setRenaming(false);
    if (!profile || !next || next === profile.name) return;
    try {
      const out = await api.profileRename(profile.fingerprint, next);
      setProfile({ ...profile, name: out.name });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  if (loading) return <Loading label="Loading profile…" />;

  const isActive = currentFp != null && currentFp === fingerprint;
  const testRunning =
    activeTest != null && (activeTest.status === "running" || activeTest.status === "pending");
  // How many iterations a top-up would still run (0 once the profile is confident).
  const topUpNeeded = Math.max(0, (minIterations ?? 0) - (profile?.iterations ?? 0));
  const isBest = bestFp != null && bestFp === fingerprint;
  // Crown-confidence context: is #1 a decisive lead or a statistical tie? The tie group is the
  // crown plus every co-leader (median lead within run-to-run noise). Surfacing this reframes a
  // profile hopping #1↔#N as "it's in an N-way tie", not "its quality swung".
  const isCoLeader = coLeaders.includes(fingerprint);
  const tieCount = coLeaders.length + (bestFp ? 1 : 0);
  const tieFps = new Set<string>([...(bestFp ? [bestFp] : []), ...coLeaders]);
  const tieOveralls = allProfiles
    .filter((p) => tieFps.has(p.fingerprint))
    .map((p) => p.overall)
    .filter((x): x is number => x != null);
  const tieBand =
    tieOveralls.length > 1
      ? `${Math.min(...tieOveralls).toFixed(1)}–${Math.max(...tieOveralls).toFixed(1)}`
      : null;
  // vs the profile's own day×hour typical — contextualizes a low recent run ("running below typical"
  // often means the network, not the profile).
  const relDelta = profile?.relative_overall?.delta_median ?? null;
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
          {/* The call sign is the headline; the settings summary and fingerprint stay
              underneath, because the name is for talking about the profile and those two
              are for knowing exactly which one it is. */}
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
            {renaming ? (
              <TextField
                size="small"
                autoFocus
                value={nameDraft}
                onChange={(e) => setNameDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void saveName();
                  if (e.key === "Escape") setRenaming(false);
                }}
                helperText="A name of your own — must be unique. Enter to save, Esc to cancel."
                sx={{ minWidth: 260 }}
              />
            ) : (
              <Typography variant="h4" sx={{ wordBreak: "break-word" }}>
                {profile?.name || profile?.label || "Profile"}
              </Typography>
            )}
            {profile && (
              <Tooltip title={renaming ? "Save this name" : "Rename this profile"}>
                <IconButton
                  size="small"
                  onClick={() => {
                    if (renaming) void saveName();
                    else {
                      setNameDraft(profile.name || "");
                      setRenaming(true);
                    }
                  }}
                >
                  {renaming ? <SaveIcon fontSize="small" /> : <EditIcon fontSize="small" />}
                </IconButton>
              </Tooltip>
            )}
          </Stack>
          {profile?.name && (
            <Typography variant="body2" color="text.secondary" sx={{ wordBreak: "break-word" }}>
              {profile.label}
            </Typography>
          )}
          <Typography variant="caption" color="text.secondary">
            {fingerprint}
          </Typography>
        </Box>
        {/* Two actions, in order of how often you want them: measuring this profile is the
            everyday, self-reversing one (it restores your settings afterwards), while
            applying it is the commitment — so the test leads and the apply sits beside it. */}
        <Stack direction="row" spacing={1} alignItems="center">
          <Tooltip
            title={`Apply this profile, benchmark ${QUICK_ITERATIONS} iteration(s), then restore your current settings. Queues behind any other firewall operation; the arrow has the longer lengths.`}
          >
            <span>
              <ButtonGroup variant="contained" disabled={starting || testRunning}>
                <Button
                  startIcon={testRunning ? <CircularProgress size={16} /> : <BoltIcon />}
                  onClick={() => void startTest(QUICK_ITERATIONS)}
                >
                  {testRunning ? "Testing…" : `Test this profile (${QUICK_ITERATIONS})`}
                </Button>
                <Button
                  size="small"
                  aria-label="other test lengths"
                  onClick={(e) => setTestMenu(e.currentTarget)}
                  sx={{ minWidth: 32, px: 0.5 }}
                >
                  <ArrowDropDownIcon fontSize="small" />
                </Button>
              </ButtonGroup>
            </span>
          </Tooltip>
          <Menu anchorEl={testMenu} open={Boolean(testMenu)} onClose={() => setTestMenu(null)}>
            <MenuItem onClick={() => void startTest(QUICK_ITERATIONS)}>
              Quick test · {QUICK_ITERATIONS} iterations
            </MenuItem>
            <MenuItem onClick={() => void startTest(QUICK_ITERATIONS * 4)}>
              Longer test · {QUICK_ITERATIONS * 4} iterations
            </MenuItem>
            {/* Only offered when there is something to top up: past the bar the top-up has
                nothing to run, and the server refuses it rather than pretending. */}
            <MenuItem onClick={() => void startTest()} disabled={topUpNeeded <= 0}>
              {topUpNeeded > 0
                ? `Test to minimum · ${topUpNeeded} more to reach ${minIterations}`
                : `Already at the ${minIterations ?? ""}-iteration minimum`}
            </MenuItem>
          </Menu>
          <Tooltip title="Write this profile's shaper settings to the firewall now. You'll preview the exact changes and confirm first.">
            <span>
              <Button
                variant="outlined"
                onClick={previewApply}
                disabled={applying || isActive}
              >
                {isActive ? "Active" : "Apply this profile"}
              </Button>
            </span>
          </Tooltip>
        </Stack>
      </Stack>

      {/* Status chips */}
      <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 2 }}>
        {isActive && <Chip color="info" label="active on firewall" />}
        {/* Crown chip, tie-aware: a decisive #1 vs a statistical tie for the lead. */}
        {isBest && tieCount > 1 ? (
          <Tooltip
            title={`The crown's median Overall lead over ${tieCount - 1} other profile${
              tieCount - 1 === 1 ? "" : "s"
            } is within run-to-run noise${tieBand ? ` (tie-group Overall ${tieBand})` : ""}. Which of these holds #1 can flip between measurements without any real change in quality.`}
          >
            <Chip color="warning" label={`tied for #1 · ${tieCount}-way (within noise)`} />
          </Tooltip>
        ) : isBest ? (
          <Chip color="success" label="best (crown) · clear lead" />
        ) : isCoLeader ? (
          <Tooltip title="This profile is statistically tied with the crown — its median Overall is within run-to-run noise of #1, so it's effectively a co-leader.">
            <Chip color="info" variant="outlined" label="tied with the crown (within noise)" />
          </Tooltip>
        ) : null}
        {relDelta != null && Math.abs(relDelta) >= 0.5 && (
          <Tooltip title="This profile's recent Overall vs its own typical for this day-of-week × hour. A negative value usually means the network was worse than usual (weather), not that the profile changed.">
            <Chip
              variant="outlined"
              color={relDelta >= 0 ? "success" : "warning"}
              label={`${relDelta >= 0 ? "+" : ""}${relDelta.toFixed(1)} vs typical`}
            />
          </Tooltip>
        )}
        {profile && !profile.confident && (
          <Chip color="warning" variant="outlined" label="limited data" />
        )}
        {profile && (
          <Chip variant="outlined" label={`${profile.iterations} iterations · ${profile.count} runs`} />
        )}
      </Stack>

      {/* The test in flight, step by step (snapshot → apply → verify → benchmark → restore).
          The jobs dropdown carries it too, but the page you started it from should say what
          it is doing without you going looking. */}
      {testRunning && (
        <Alert severity="info" icon={<CircularProgress size={16} />} sx={{ mb: 2 }}>
          Testing this profile ({activeTest?.iterations} iteration
          {activeTest?.iterations === 1 ? "" : "s"}) —{" "}
          {activeTest?.stage ??
            (activeTest?.status === "pending" ? "queued behind another operation" : "starting…")}
          . Your current settings are restored when it finishes.
        </Alert>
      )}

      {/* Standings: this profile's rank (1 = best) per Overall + headline axis, green→red. */}
      {standings.length > 0 && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="h6" gutterBottom>
              Standings
            </Typography>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
              Rank among all {standings[0].total || 0} measured profiles (1 = best).
              <HelpTip title="For the Overall and each crown metric the current methodology scores on. The red arrow is how far behind #1 it is, as a percent." />
            </Typography>
            {/* Crown lead vs noise: the measured signal-to-noise behind #1 — is the lead real, or is
                the top a statistical tie? Numbers, not adjectives. */}
            {crownConf && crownConf.gap_to_runner_up != null && crownConf.noise_threshold != null && (
              <Alert severity={crownConf.clear_lead ? "success" : "info"} icon={false} sx={{ mb: 1.5, py: 0.25 }}>
                <Typography variant="caption">
                  {crownConf.clear_lead ? (
                    <>
                      <b>Crown lead is real:</b> +{crownConf.gap_to_runner_up.toFixed(2)} over the
                      runner-up, past the {crownConf.noise_threshold.toFixed(2)} noise bar.
                    </>
                  ) : (
                    <>
                      <b>Crown is a statistical tie:</b> +{crownConf.gap_to_runner_up.toFixed(2)} over
                      the runner-up is within noise (needs &gt; {crownConf.noise_threshold.toFixed(2)}),{" "}
                      {crownConf.co_leader_count} co-leader{crownConf.co_leader_count === 1 ? "" : "s"}.
                      More runs could break it.
                    </>
                  )}
                  <HelpTip
                    title={`Crown Overall ${crownConf.overall.toFixed(1)} ± ${crownConf.overall_se.toFixed(2)} SE. The bar is ${crownConf.sigma}σ of the pooled run-to-run noise, so it tightens as runs accrue.`}
                  />
                </Typography>
              </Alert>
            )}
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

      {/* ── In the ring: this profile's head-to-head record ────────────────────────────
          The standings above are the OBSERVATIONAL verdict — everything this profile has
          ever measured, pooled. This is the controlled one: who it actually beat, in
          interleaved back-to-back rounds that shared their weather. They answer different
          questions, and their disagreement is the whole reason the ladder exists, so both
          belong on the page that is about this one profile. Everything here is signed from
          this profile's own side. */}
      {ring && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
              <Typography variant="h6" sx={{ flexGrow: 1 }}>
                In the ring
              </Typography>
              {ring.is_champion && <Chip color="warning" size="small" label="reigning champion" />}
              <Button size="small" onClick={() => navigate("/duels")}>
                Dueling Champions
              </Button>
            </Stack>

            {!ring.in_ring ? (
              <Typography variant="body2" color="text.secondary">
                This profile hasn&apos;t fought a duel yet
                {ring.sessions_analyzed > 0
                  ? ` — the ladder has run ${ring.sessions_analyzed} session${
                      ring.sessions_analyzed === 1 ? "" : "s"
                    } without matching it.`
                  : " — no duel sessions on record yet."}
                <HelpTip title="The ladder races the profiles most likely to unseat the leader, so an untested profile gets the ring once its measured ceiling makes it a threat." />
              </Typography>
            ) : (
              <>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                  Head-to-head record over {ring.matchups_analyzed} match
                  {ring.matchups_analyzed === 1 ? "" : "es"} in {ring.sessions_analyzed} session
                  {ring.sessions_analyzed === 1 ? "" : "s"}.
                  <HelpTip
                    title={`What this profile has beaten, not what it averaged. Ranked on the ring's fitted strength (${
                      ring.ranked_by === "rating_floor"
                        ? `rating − ${ring.rank_sigma ?? 1} SE, so a record has to be measured before it can lead`
                        : ring.ranked_by
                    }).`}
                  />
                </Typography>

                {ring.record && (
                  <Box
                    sx={{
                      display: "grid",
                      gap: 1.5,
                      mb: 2,
                      gridTemplateColumns: { xs: "repeat(2, 1fr)", sm: "repeat(4, 1fr)" },
                    }}
                  >
                    {[
                      {
                        label: "Duel rank",
                        value: `#${ring.record.rank}`,
                        sub: `of ${ring.rank_of} in the ring`,
                        color: rankColor(ring.record.rank, ring.rank_of),
                        tip: "Where this profile sits in the head-to-head league table — a different ranking from the pooled standings above, earned only against opponents it actually faced.",
                      },
                      {
                        label: "Rating",
                        value: ring.record.rating != null ? Math.round(ring.record.rating) : "—",
                        sub:
                          ring.record.rating_se != null
                            ? `± ${Math.round(ring.record.rating_se)} · ${ring.record.rating_pairs ?? 0} rounds`
                            : "no fit yet",
                        color: undefined,
                        tip: "Bradley-Terry strength fitted to every round on the ledger (Elo scale, 1500 = middle of the field). Beating a strong profile moves it a lot; beating a weak one barely at all.",
                      },
                      {
                        label: "Record",
                        value: `${ring.record.wins}-${ring.record.losses}-${ring.record.draws}`,
                        sub: `W-L-D · ${ring.record.opponents} opponent${ring.record.opponents === 1 ? "" : "s"}`,
                        color: undefined,
                        tip: "Matches won, lost and drawn. A draw means the two profiles were practically equal, not that the match was inconclusive.",
                      },
                      {
                        label: "Rounds",
                        value: `${ring.record.pair_wins}-${ring.record.pair_losses}`,
                        sub:
                          ring.record.pair_win_rate != null
                            ? `${Math.round(ring.record.pair_win_rate * 100)}% of rounds won`
                            : "—",
                        color: undefined,
                        tip: "Individual interleaved rounds won and lost — the unit of evidence the rating is fitted to, so a hard-fought 12-8 counts for more than a 3-0 snap.",
                      },
                    ].map((b) => (
                      <Tooltip key={b.label} title={b.tip}>
                        <Box
                          sx={{
                            p: 1.5,
                            borderRadius: 1,
                            border: 1,
                            borderColor: "divider",
                            textAlign: "center",
                          }}
                        >
                          <Typography variant="overline" color="text.secondary" sx={{ display: "block" }}>
                            {b.label}
                          </Typography>
                          <Typography sx={{ fontWeight: 800, fontSize: "1.5rem", lineHeight: 1.15, color: b.color }}>
                            {b.value}
                          </Typography>
                          <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                            {b.sub}
                          </Typography>
                        </Box>
                      </Tooltip>
                    ))}
                  </Box>
                )}

                <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 2 }}>
                  {ring.record?.rating_provisional && (
                    <Tooltip
                      title={`Fewer than ${ring.provisional_pairs ?? "enough"} rounds, or only one opponent — the rating is mostly the prior talking. It still ranks; it just hasn't been earned yet.`}
                    >
                      <Chip size="small" variant="outlined" color="warning" label="provisional rating" />
                    </Tooltip>
                  )}
                  {ring.record?.median_margin != null && (
                    <Tooltip title="Median Overall-point margin across this profile's rounds, from its own side. Positive means it was the better profile in the ring, by that many points.">
                      <Chip
                        size="small"
                        variant="outlined"
                        color={ring.record.median_margin >= 0 ? "success" : "error"}
                        label={`${ring.record.median_margin >= 0 ? "+" : ""}${ring.record.median_margin.toFixed(2)} median margin`}
                      />
                    </Tooltip>
                  )}
                  {(ring.record?.championships ?? 0) > 0 && (
                    <Chip
                      size="small"
                      variant="outlined"
                      label={`${ring.record!.championships} session${ring.record!.championships === 1 ? "" : "s"} ended holding the belt`}
                    />
                  )}
                  {ring.champion && !ring.is_champion && (
                    <Chip
                      size="small"
                      variant="outlined"
                      onClick={() => navigate(`/profiles/${encodeURIComponent(ring.champion!.fingerprint)}`)}
                      label={`belt: ${ring.champion.name ?? ring.champion.label ?? ring.champion.fingerprint}`}
                    />
                  )}
                </Stack>

                {/* Per-opponent: the record against each profile it has faced, WITH what
                    the pooled verdict thinks of that profile.

                    This was a wall of 184 W-L-D chips, most of them "0-0-2" — pairings the
                    ring never decided. That buries the ~40 real results among the noise,
                    and it still couldn't answer the question a #1-in-the-ring /
                    #113-on-Overall profile actually raises: did it beat profiles Overall
                    rates ABOVE it? Answering that meant opening each opponent's page one at
                    a time. The Overall and the signed gap are on every row now, decided
                    pairings lead, and the ones where the two verdicts disagree lead those. */}
                {ring.opponents.length > 0 && (
                  <Box sx={{ mb: 2 }}>
                    <Stack
                      direction={{ xs: "column", sm: "row" }}
                      spacing={1}
                      alignItems={{ xs: "flex-start", sm: "baseline" }}
                      justifyContent="space-between"
                      sx={{ mb: 1 }}
                    >
                      <Typography variant="subtitle2">Against each opponent</Typography>
                      <FormControlLabel
                        control={
                          <Switch
                            size="small"
                            checked={showUndecided}
                            onChange={(e) => setShowUndecided(e.target.checked)}
                          />
                        }
                        label={
                          <Typography variant="caption" color="text.secondary">
                            Show undecided ({ring.versus_overall?.undecided_opponents ?? 0})
                          </Typography>
                        }
                      />
                    </Stack>

                    {/* The headline. "Beat 12 profiles Overall ranks above it" is the whole
                        finding; without it the reader has to derive it from the rows. */}
                    {ring.versus_overall && (
                      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
                        Of {ring.versus_overall.decided_opponents} decided pairing
                        {ring.versus_overall.decided_opponents === 1 ? "" : "s"}, it{" "}
                        <b>beat {ring.versus_overall.beat_higher_overall}</b> profile
                        {ring.versus_overall.beat_higher_overall === 1 ? "" : "s"} that score
                        higher on Overall, and <b>lost to {ring.versus_overall.lost_to_lower_overall}</b>{" "}
                        that score lower.
                        <HelpTip title="That disagreement is what running two verdicts is for: the ring measures profiles against each other under shared weather, Overall pools everything each has ever measured." />
                      </Typography>
                    )}

                    <Stack spacing={0.5}>
                      {visibleOpponents.map((o) => (
                        <Stack
                          key={o.fingerprint}
                          direction="row"
                          spacing={1}
                          alignItems="center"
                          sx={{
                            py: 0.5,
                            borderBottom: 1,
                            borderColor: "divider",
                            opacity: o.decisive ? 1 : 0.55,
                          }}
                        >
                          <Link
                            component="button"
                            underline="hover"
                            onClick={() =>
                              navigate(`/profiles/${encodeURIComponent(o.fingerprint)}`)
                            }
                            sx={{ font: "inherit", textAlign: "left", flex: 1, minWidth: 0 }}
                          >
                            <Typography variant="body2" noWrap>
                              {o.name ?? o.fingerprint.slice(0, 8)}
                            </Typography>
                          </Link>
                          <Tooltip
                            title={`${o.pairs} round${o.pairs === 1 ? "" : "s"}${
                              o.median_margin != null
                                ? ` · median margin ${o.median_margin >= 0 ? "+" : ""}${o.median_margin.toFixed(2)} Overall points from this profile's side`
                                : ""
                            }`}
                          >
                            <Chip
                              size="small"
                              variant="outlined"
                              color={
                                o.wins > o.losses
                                  ? "success"
                                  : o.losses > o.wins
                                    ? "error"
                                    : "default"
                              }
                              label={`${o.wins}-${o.losses}-${o.draws}`}
                              sx={{ minWidth: 68 }}
                            />
                          </Tooltip>
                          {/* Their Overall, and the gap from this profile's side. A win
                              against a red gap is a profile beating one that outscores it. */}
                          <Typography
                            variant="caption"
                            color="text.secondary"
                            sx={{ width: 52, textAlign: "right" }}
                          >
                            {o.overall != null ? o.overall.toFixed(1) : "—"}
                          </Typography>
                          <Typography
                            variant="caption"
                            sx={{ width: 56, textAlign: "right" }}
                            color={
                              o.overall_delta == null
                                ? "text.disabled"
                                : o.overall_delta > 0
                                  ? "success.main"
                                  : "error.main"
                            }
                            title="This profile's Overall minus theirs"
                          >
                            {o.overall_delta == null
                              ? "—"
                              : `${o.overall_delta > 0 ? "+" : ""}${o.overall_delta.toFixed(1)}`}
                          </Typography>
                        </Stack>
                      ))}
                    </Stack>
                    {visibleOpponents.length < shownOpponents.length && (
                      <Button size="small" sx={{ mt: 1 }} onClick={() => setOpponentsShown((n) => n + 40)}>
                        Show more ({shownOpponents.length - visibleOpponents.length} left)
                      </Button>
                    )}
                  </Box>
                )}

                {/* The tape: every bout this profile fought, newest first. */}
                <Typography variant="subtitle2" gutterBottom>
                  Bouts ({ring.bouts.length})
                </Typography>
                {narrow ? (
                  <Stack spacing={1}>
                    {ring.bouts.slice(0, boutsShown).map((b, i) => (
                      <Box
                        key={`${b.duel_id}-${b.opponent}-${i}`}
                        sx={{ p: 1, borderRadius: 1, border: 1, borderColor: "divider" }}
                      >
                        <Stack direction="row" spacing={1} alignItems="center">
                          <Link
                            component="button"
                            type="button"
                            underline="hover"
                            color="inherit"
                            sx={{ font: "inherit", fontWeight: 600, textAlign: "left", minWidth: 0 }}
                            onClick={() => navigate(`/profiles/${encodeURIComponent(b.opponent)}`)}
                          >
                            {b.opponent_name ?? b.opponent_label}
                          </Link>
                          <Box sx={{ flexGrow: 1 }} />
                          <Chip
                            size="small"
                            color={BOUT_COLOR[b.result]}
                            variant={b.result === "draw" ? "outlined" : "filled"}
                            label={b.result}
                          />
                        </Stack>
                        <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                          {b.role} · {b.pair_wins}–{b.pair_losses} rounds
                          {b.margin != null && (
                            <>
                              {" · "}
                              <Typography
                                component="span"
                                variant="caption"
                                sx={{ fontWeight: 700, color: b.margin >= 0 ? "success.main" : "error.main" }}
                              >
                                {b.margin >= 0 ? "+" : ""}
                                {b.margin.toFixed(2)}
                              </Typography>
                            </>
                          )}
                          {b.finished_at ? ` · ${fmtTimeShort(b.finished_at)}` : ` · duel #${b.duel_id}`}
                        </Typography>
                        {b.reason && (
                          <Typography variant="caption" color="text.disabled" sx={{ display: "block" }}>
                            {b.reason}
                          </Typography>
                        )}
                      </Box>
                    ))}
                  </Stack>
                ) : (
                <TableContainer>
                  <Table size="small">
                    <TableHead>
                      <TableRow>
                        <TableCell>When</TableCell>
                        <TableCell>Opponent</TableCell>
                        <TableCell>Corner</TableCell>
                        <TableCell>Result</TableCell>
                        <TableCell align="right">Rounds</TableCell>
                        <TableCell align="right">Margin</TableCell>
                        <TableCell>What ended it</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {ring.bouts.slice(0, boutsShown).map((b, i) => (
                        <TableRow key={`${b.duel_id}-${b.opponent}-${i}`} hover>
                          <TableCell sx={{ whiteSpace: "nowrap" }}>
                            {b.finished_at ? fmtDateTime(b.finished_at) : `duel #${b.duel_id}`}
                          </TableCell>
                          <TableCell>
                            <Link
                              component="button"
                              type="button"
                              underline="hover"
                              color="inherit"
                              onClick={() => navigate(`/profiles/${encodeURIComponent(b.opponent)}`)}
                            >
                              {b.opponent_name ?? b.opponent_label}
                            </Link>
                          </TableCell>
                          <TableCell>
                            <Tooltip
                              title={
                                b.role === "defended"
                                  ? "This profile held the belt and defended it."
                                  : "This profile challenged for the belt."
                              }
                            >
                              <Typography variant="caption" color="text.secondary">
                                {b.role}
                              </Typography>
                            </Tooltip>
                          </TableCell>
                          <TableCell>
                            <Chip
                              size="small"
                              color={BOUT_COLOR[b.result]}
                              variant={b.result === "draw" ? "outlined" : "filled"}
                              label={b.result}
                            />
                          </TableCell>
                          <TableCell align="right" sx={{ whiteSpace: "nowrap" }}>
                            {b.pair_wins}–{b.pair_losses}
                          </TableCell>
                          <TableCell align="right">
                            {b.margin == null ? (
                              "—"
                            ) : (
                              <Typography
                                component="span"
                                variant="body2"
                                sx={{ fontWeight: 700, color: b.margin >= 0 ? "success.main" : "error.main" }}
                              >
                                {b.margin >= 0 ? "+" : ""}
                                {b.margin.toFixed(2)}
                              </Typography>
                            )}
                          </TableCell>
                          <TableCell>
                            <Typography variant="caption" color="text.secondary">
                              {b.reason ?? "—"}
                            </Typography>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableContainer>
                )}
                {ring.bouts.length > boutsShown && (
                  <Button size="small" sx={{ mt: 1 }} onClick={() => setBoutsShown((n) => n + 20)}>
                    Show earlier bouts ({ring.bouts.length - boutsShown} more)
                  </Button>
                )}
              </>
            )}
          </CardContent>
        </Card>
      )}

      {/* Data-integrity audit: prove old and new runs are like-for-like by re-deriving each from
          its immutable raw and diffing against the stored value. */}
      <FoldCard
        title="Data integrity"
        summary={
          <>
            Do old and new runs still measure the same thing?
            <HelpTip title="Re-derives this profile's oldest and newest runs from their stored raw and checks the saved metrics still reproduce. If old runs drift while new ones don't, history was computed under a formula that has since changed and needs a re-derive. Read-only." />
          </>
        }
        actions={
          <Button size="small" variant="outlined" onClick={runAudit} disabled={auditing}>
            {auditing ? "Verifying…" : "Verify old vs new"}
          </Button>
        }
        openWhen={auditing || !!audit || !!auditErr}
      >
          {!audit && !auditErr && (
            <Typography variant="body2" color="text.secondary">
              {auditing ? "Re-deriving the oldest and newest runs from raw…" : (
                <>
                  Press <b>Verify old vs new</b> to run the check.
                </>
              )}
            </Typography>
          )}
          {auditErr && <Alert severity="error">{auditErr}</Alert>}
          {audit && (
            <Box>
              <Alert severity={audit.consistent ? "success" : audit.stale_history ? "warning" : "error"} sx={{ mb: 1 }}>
                {audit.consistent
                  ? `Like-for-like: all sampled runs reproduce exactly from raw (derivation ${audit.current_derivation}).`
                  : audit.stale_history
                    ? "Stale history: older runs use an outdated formula. Run Re-derive on the Methodology page."
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
              {/* Ingredients check: did we collect the SAME raw old vs new? (URLs / LoAF / composition) */}
              {audit.collection && (
                <Box sx={{ mt: 2, pt: 1.5, borderTop: 1, borderColor: "divider" }}>
                  <Typography variant="subtitle2" gutterBottom>
                    Raw collection (ingredients)
                  </Typography>
                  <Alert severity={audit.collection.changed ? "warning" : "success"} sx={{ mb: audit.collection.changed ? 1 : 0 }}>
                    {audit.collection.changed
                      ? "What was collected changed between old and new runs, so they aren't measuring the same thing."
                      : "Same ingredients: old and new runs loaded the same URLs with the same coverage."}
                  </Alert>
                  {audit.collection.changed && (
                    <Stack spacing={0.25} sx={{ mt: 0.5 }}>
                      {audit.collection.urls_added.length > 0 && (
                        <Typography variant="body2" color="warning.main">
                          URLs only in new runs: {audit.collection.urls_added.join(", ")}
                        </Typography>
                      )}
                      {audit.collection.urls_removed.length > 0 && (
                        <Typography variant="body2" color="warning.main">
                          URLs only in old runs: {audit.collection.urls_removed.join(", ")}
                        </Typography>
                      )}
                      {audit.collection.loaf_changed && (
                        <Typography variant="body2" color="warning.main">
                          LoAF coverage changed: old {Math.round(audit.collection.loaf_present.old * 100)}% → new{" "}
                          {Math.round(audit.collection.loaf_present.new * 100)}% of observations
                        </Typography>
                      )}
                      {Object.entries(audit.collection.resource_shift).map(([url, s]) => (
                        <Typography key={url} variant="body2" color="warning.main">
                          {url}: page composition shifted {s.old} → {s.new} median resources
                        </Typography>
                      ))}
                    </Stack>
                  )}
                </Box>
              )}
              <Typography variant="caption" color="text.disabled" sx={{ display: "block", mt: 1 }}>
                {audit.total_runs} total run{audit.total_runs === 1 ? "" : "s"} · sampled oldest &amp; newest
              </Typography>
            </Box>
          )}
      </FoldCard>

      <Box sx={{ display: "grid", gap: 2 }}>
        {profile?.metrics && profile.metrics["nav_response"] != null && (
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                Load waterfall (median)
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 2 }}>
                Judge this profile on the amber <b>Delivery</b> phase: the one your shaper moves.
                <HelpTip title="Setup up to first byte (DNS/TCP/TLS/TTFB) is weather, not shaping. Delivery (first byte → response done) is body delivery through the queue. Client render is CPU and should match across profiles." />
              </Typography>
              <Waterfall metrics={profile.metrics} />
            </CardContent>
          </Card>
        )}

        {pauses && pauses.urls.length > 0 && (
          <FoldCard
            sx={{ mb: 0 }}
            title="Where's the pause?"
            summary={
              <>
                The longest void per page load, median across {pauses.runs} run
                {pauses.runs === 1 ? "" : "s"}.
                <HelpTip title="Where nothing finished loading: how long, where in the load it falls, and whether it's network (the part your shaper moves) or render (main thread, shaping-immune). The crown's network-stall leg is built on this." />
              </>
            }
          >
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
          </FoldCard>
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

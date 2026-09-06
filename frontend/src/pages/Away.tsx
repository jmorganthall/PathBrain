import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import LinearProgress from "@mui/material/LinearProgress";
import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import Stack from "@mui/material/Stack";
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";
import FlightTakeoffIcon from "@mui/icons-material/FlightTakeoff";
import HomeIcon from "@mui/icons-material/Home";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import StopIcon from "@mui/icons-material/Stop";

import { api, tzOffsetMinutes } from "../api/client";
import type {
  PortableCompareMetric,
  PortableHome,
  PortableMetricMeta,
  PortableRecipe,
  PortableRun,
} from "../api/types";
import { Blurb } from "../components/Explain";
import { fmtDateTime } from "../utils/format";
import { clientInfo, deviceId, egressIp, runPortableTest } from "../utils/portableTest";
import type { PortableProgress } from "../utils/portableTest";

const VENUE_KEY = "pathbrain.portable.venue";
const MODE_KEY = "pathbrain.portable.mode"; // "auto" | "home" | "away"
const LABEL_KEY = "pathbrain.portable.device_label";

const ORIGIN_PHASES: { key: string; label: string }[] = [
  { key: "dns_ms", label: "DNS" },
  { key: "tcp_ms", label: "TCP" },
  { key: "tls_ms", label: "TLS" },
  { key: "ttfb_ms", label: "TTFB" },
  { key: "download_ms", label: "Download" },
];

function fmtValue(v: number | null | undefined, unit: string): string {
  if (v == null || Number.isNaN(v)) return "—";
  if (unit === "ms") return v >= 100 ? `${Math.round(v)} ms` : `${v.toFixed(1)} ms`;
  if (unit === "Mbit/s") return `${v.toFixed(1)} Mbit/s`;
  return v.toFixed(2);
}

function fmtDelta(m: PortableCompareMetric, unit: string): string {
  const sign = m.delta > 0 ? "+" : "";
  const abs = unit === "ms" ? (Math.abs(m.delta) >= 100 ? Math.round(m.delta).toString() : m.delta.toFixed(1)) : m.delta.toFixed(2);
  const pct = m.pct != null && Number.isFinite(m.pct) ? ` (${m.pct > 0 ? "+" : ""}${m.pct.toFixed(0)}%)` : "";
  return `${sign}${abs}${unit === "ms" ? " ms" : unit === "Mbit/s" ? " Mbit/s" : ""}${pct}`;
}

function verdictColor(v: PortableCompareMetric["verdict"]): "success" | "error" | "default" {
  return v === "better" ? "success" : v === "worse" ? "error" : "default";
}

function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

function readStorage(key: string, fallback: string): string {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function writeStorage(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

// ── result pieces ────────────────────────────────────────────────────────────

function ScoreLine({ run }: { run: PortableRun }) {
  const cmp = run.compare;
  const score = cmp?.available ? cmp.score : null;
  return (
    <Stack direction="row" spacing={3} alignItems="baseline" flexWrap="wrap" useFlexGap>
      <Box>
        <Typography variant="overline" color="text.secondary">
          {run.is_home ? "Home (this run)" : run.venue || "Away"}
        </Typography>
        <Typography variant="h3" sx={{ fontWeight: 700, lineHeight: 1 }}>
          {run.score != null ? Math.round(run.score) : "—"}
        </Typography>
      </Box>
      {score && (
        <>
          <Box>
            <Typography variant="overline" color="text.secondary">
              Home median
            </Typography>
            <Typography variant="h3" sx={{ fontWeight: 400, lineHeight: 1 }}>
              {Math.round(score.home_median)}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              IQR {score.home_p25 != null ? Math.round(score.home_p25) : "—"}–
              {score.home_p75 != null ? Math.round(score.home_p75) : "—"} · {score.n} runs
            </Typography>
          </Box>
          <Chip
            size="medium"
            color={score.delta > 0 ? "success" : score.delta < 0 ? "error" : "default"}
            label={`${score.delta > 0 ? "+" : ""}${score.delta.toFixed(1)} vs home`}
            sx={{ fontWeight: 600 }}
          />
        </>
      )}
    </Stack>
  );
}

function Provenance({ run }: { run: PortableRun }) {
  const cmp = run.compare;
  if (!cmp) return null;
  const p = cmp.provenance;
  if (!cmp.available) {
    return (
      <Alert severity="info" sx={{ mt: 1.5 }}>
        No "vs home" yet: {cmp.reason} ({p.home_runs_on_device} of {p.min_home_runs} on this device)
      </Alert>
    );
  }
  const parts: string[] = [];
  parts.push(`vs ${p.home_runs_used} home run${p.home_runs_used === 1 ? "" : "s"} from this device`);
  parts.push("same test version");
  if (p.time_rung_label) parts.push(p.time_rung_label);
  if (p.profile) parts.push(`on ${p.profile.summary || p.profile.fingerprint.slice(0, 8)}`);
  else if (p.profile_note) parts.push(p.profile_note);
  return (
    <Box sx={{ mt: 1.5 }}>
      <Typography variant="body2" color="text.secondary">
        {parts.join(" · ")}
      </Typography>
      {(p.dropped_resources?.length ?? 0) > 0 && (
        <Typography variant="caption" color="warning.main" component="div">
          Compared without {p.dropped_resources!.join(", ")}: not completed on both sides, so dropped from both.
        </Typography>
      )}
      {(p.home_runs_dropped ?? 0) > 0 && (
        <Typography variant="caption" color="text.secondary" component="div">
          {p.home_runs_dropped} home run(s) set aside because they lacked one of the shared resources.
        </Typography>
      )}
    </Box>
  );
}

function MetricTable({ run, meta }: { run: PortableRun; meta: PortableMetricMeta[] }) {
  const cmp = run.compare;
  const rows = meta.filter((m) => run.metrics[m.key] != null);
  return (
    <Box sx={{ overflowX: "auto", mt: 1 }}>
      <Table size="small" sx={{ minWidth: 520 }}>
        <TableHead>
          <TableRow>
            <TableCell>Metric</TableCell>
            <TableCell align="right">{run.is_home ? "This run" : "Away"}</TableCell>
            <TableCell align="right">Home median</TableCell>
            <TableCell align="right">Home IQR</TableCell>
            <TableCell align="right">vs home</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((m) => {
            const c = cmp?.available ? cmp.metrics?.[m.key] : undefined;
            return (
              <TableRow key={m.key} hover>
                <TableCell>
                  <Tooltip title={m.lower_is_better ? "lower is better" : "higher is better"}>
                    <span>
                      {m.label}
                      {m.scored ? "" : " ·"}
                    </span>
                  </Tooltip>
                </TableCell>
                <TableCell align="right" sx={{ fontVariantNumeric: "tabular-nums" }}>
                  {fmtValue(c ? c.away : run.metrics[m.key], m.unit)}
                </TableCell>
                <TableCell align="right" sx={{ fontVariantNumeric: "tabular-nums" }}>
                  {c ? fmtValue(c.home_median, m.unit) : "—"}
                </TableCell>
                <TableCell align="right" sx={{ fontVariantNumeric: "tabular-nums", color: "text.secondary" }}>
                  {c ? `${fmtValue(c.home_p25, m.unit)} – ${fmtValue(c.home_p75, m.unit)}` : "—"}
                </TableCell>
                <TableCell align="right">
                  {c ? (
                    <Chip size="small" color={verdictColor(c.verdict)} variant={c.verdict === "within" ? "outlined" : "filled"} label={fmtDelta(c, m.unit)} />
                  ) : (
                    "—"
                  )}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
        "vs home" is read against home's own run-to-run spread: inside the IQR is <em>within</em> what home does
        itself; past its worse edge is <em>worse</em>. Metrics marked · are shown but not scored.
      </Typography>
    </Box>
  );
}

function OriginTable({ run }: { run: PortableRun }) {
  const origins = Object.keys(run.per_origin || {});
  if (!origins.length) return null;
  const cmp = run.compare;
  return (
    <Box sx={{ overflowX: "auto", mt: 2 }}>
      <Typography variant="subtitle2" gutterBottom>
        Connection setup per origin
      </Typography>
      <Table size="small" sx={{ minWidth: 520 }}>
        <TableHead>
          <TableRow>
            <TableCell>Origin</TableCell>
            {ORIGIN_PHASES.map((p) => (
              <TableCell key={p.key} align="right">
                {p.label}
              </TableCell>
            ))}
          </TableRow>
        </TableHead>
        <TableBody>
          {origins.map((o) => (
            <TableRow key={o} hover>
              <TableCell sx={{ fontFamily: "monospace", fontSize: 12 }}>{o}</TableCell>
              {ORIGIN_PHASES.map((p) => {
                const v = run.per_origin[o]?.[p.key];
                const c = cmp?.available ? cmp.per_origin?.[o]?.[p.key] : undefined;
                return (
                  <TableCell key={p.key} align="right" sx={{ fontVariantNumeric: "tabular-nums" }}>
                    {fmtValue(v, "ms")}
                    {c && (
                      <Typography
                        component="div"
                        variant="caption"
                        color={c.verdict === "worse" ? "error.main" : c.verdict === "better" ? "success.main" : "text.secondary"}
                      >
                        home {fmtValue(c.home_median, "ms")}
                      </Typography>
                    )}
                  </TableCell>
                );
              })}
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
        From the first new connection to each origin; only origins that send Timing-Allow-Origin expose these.
      </Typography>
    </Box>
  );
}

function Failures({ run }: { run: PortableRun }) {
  const failed = Object.entries(run.coverage?.resources_failed || {});
  if (!failed.length) return null;
  return (
    <Alert severity="warning" sx={{ mt: 2 }}>
      {failed.length} resource{failed.length === 1 ? "" : "s"} did not load on this network:{" "}
      {failed.map(([id, err]) => `${id} (${err})`).join(", ")}. They are excluded from both sides of the comparison.
    </Alert>
  );
}

// ── the page ─────────────────────────────────────────────────────────────────

export default function Away() {
  const [recipe, setRecipe] = useState<PortableRecipe | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<PortableProgress | null>(null);
  const [result, setResult] = useState<PortableRun | null>(null);
  const [history, setHistory] = useState<PortableRun[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  const device = useMemo(() => deviceId(), []);
  const [label, setLabel] = useState(() => readStorage(LABEL_KEY, ""));
  const [venue, setVenue] = useState(() => readStorage(VENUE_KEY, ""));
  type Mode = "auto" | "home" | "away";
  const [mode, setMode] = useState<Mode>(() => {
    const m = readStorage(MODE_KEY, "auto");
    return m === "home" || m === "away" ? m : "auto";
  });
  // Home detection: the device's public egress vs the home WAN address (see portable.decide_home).
  const [homeInfo, setHomeInfo] = useState<PortableHome | null>(null);
  const [egress, setEgress] = useState<string | null | undefined>(undefined); // undefined = looking
  const detected: boolean | null =
    homeInfo?.home_ip && egress ? egress === homeInfo.home_ip : null;
  const isHome = mode === "home" ? true : mode === "away" ? false : detected;

  const detect = useCallback(async () => {
    setEgress(undefined);
    try {
      const info = await api.portableHome();
      setHomeInfo(info);
      let ip = info.lookup_url ? await egressIp(info.lookup_url) : null;
      // Fallback where the lookup is blocked: the address PathBrain saw this request come
      // from — meaningful only when it is a public address (a LAN or tunnel source says
      // nothing about where the device's internet traffic leaves).
      if (!ip && info.request_ip_public) ip = info.request_ip;
      setEgress(ip);
    } catch {
      setEgress(null);
    }
  }, []);

  const loadHistory = useCallback(async () => {
    try {
      setHistory(await api.portableRuns(device, 40));
    } catch {
      /* transient */
    }
  }, [device]);

  useEffect(() => {
    api
      .portableRecipe()
      .then(setRecipe)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    loadHistory();
    detect();
    // Hydrate the label from the server if this device is already known there.
    api
      .portableDevices()
      .then((devs) => {
        const me = devs.find((d) => d.device_id === device);
        if (me?.label && !readStorage(LABEL_KEY, "")) {
          setLabel(me.label);
          writeStorage(LABEL_KEY, me.label);
        }
      })
      .catch(() => undefined);
  }, [device, loadHistory, detect]);

  const homeCount = history.filter((r) => r.is_home && r.instrument_version === recipe?.instrument_version).length;
  const minHome = recipe?.min_home_runs ?? 5;

  const start = async () => {
    if (!recipe) return;
    setError(null);
    setResult(null);
    setRunning(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    writeStorage(VENUE_KEY, venue);
    writeStorage(MODE_KEY, mode);
    writeStorage(LABEL_KEY, label);
    try {
      const raw = await runPortableTest(recipe, { signal: ctrl.signal, onProgress: setProgress });
      const run = await api.portableUpload({
        device_id: device,
        device_label: label.trim() || null,
        venue: isHome ? null : venue.trim() || null,
        is_home: mode === "auto" ? null : mode === "home",
        egress_ip: egress ?? null,
        instrument_version: recipe.instrument_version,
        tz_offset_minutes: tzOffsetMinutes(),
        client: clientInfo(),
        raw,
      });
      setResult(run);
      await loadHistory();
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setRunning(false);
      setProgress(null);
      abortRef.current = null;
    }
  };

  const stop = () => abortRef.current?.abort();

  const open = async (id: number) => {
    try {
      setResult(await api.portableRun(id));
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const remove = async (id: number) => {
    try {
      await api.portableDelete(id);
      if (result?.id === id) setResult(null);
      await loadHistory();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const saveLabel = async () => {
    writeStorage(LABEL_KEY, label);
    if (history.length) {
      try {
        await api.portableDeviceRename(device, label.trim() || null);
        await loadHistory();
      } catch {
        /* the next upload carries it anyway */
      }
    }
  };

  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
        <FlightTakeoffIcon color="primary" />
        <Typography variant="h5" sx={{ fontWeight: 700 }}>
          Away test
        </Typography>
      </Stack>
      <Blurb
        more={
          <>
            A browser tab can't load the real sites the home test measures and read their timing, so this
            is a <em>different instrument</em>: a synthetic waterfall of public CDN objects, one streamed
            download and a burst of round trips. It is never put on the methodology's Overall scale. Its one
            comparison is <strong>vs home</strong>, and only against directly comparable data: the same
            device, the same test version, home runs on one firewall profile, the nearest time of day with
            enough runs, and only the resources both sides completed. Home is <em>detected</em>: this device's
            public address is compared with PathBrain's own, so a run counts as home only when its traffic
            actually leaves through the tuned firewall (a phone on cellular on the couch is not home for this
            purpose). Run it at home a few times first so this device has something to compare against.
          </>
        }
      >
        "This isn't home, but here is how it stood up." Measure this network from this device and compare it
        with the same device's runs at home.
      </Blurb>

      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack spacing={1.5}>
            <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} alignItems={{ sm: "center" }}>
              <TextField
                size="small"
                label="This device"
                placeholder="e.g. Josh's phone"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                onBlur={saveLabel}
                disabled={running}
                sx={{ minWidth: 200 }}
                helperText={`id ${shortId(device)}`}
              />
              <TextField
                size="small"
                label="Where are you?"
                placeholder="Hotel Wi-Fi, Denver"
                value={venue}
                onChange={(e) => setVenue(e.target.value)}
                disabled={running || isHome === true}
                sx={{ minWidth: 220, flex: 1 }}
                helperText={isHome === true ? "home runs are the reference" : " "}
              />
              <Box>
                <ToggleButtonGroup
                  exclusive
                  size="small"
                  value={mode}
                  onChange={(_e, v: Mode | null) => v && setMode(v)}
                  disabled={running}
                  aria-label="home or away"
                >
                  <ToggleButton value="auto">Auto</ToggleButton>
                  <ToggleButton value="home">
                    <HomeIcon fontSize="small" sx={{ mr: 0.5 }} /> Home
                  </ToggleButton>
                  <ToggleButton value="away">
                    <FlightTakeoffIcon fontSize="small" sx={{ mr: 0.5 }} /> Away
                  </ToggleButton>
                </ToggleButtonGroup>
                <Typography variant="caption" color="text.secondary" component="div" sx={{ mt: 0.5 }}>
                  {egress === undefined
                    ? "Detecting where you are…"
                    : detected === true
                      ? `Detected: home — this device leaves the internet through the same address as PathBrain (${egress}).`
                      : detected === false
                        ? `Detected: away — this device's public address (${egress}) is not home's (${homeInfo?.home_ip}).`
                        : homeInfo?.home_ip
                          ? "Couldn't read this device's public address (the lookup may be blocked here) — choose Home or Away."
                          : `Home address unknown${homeInfo?.error ? ` (${homeInfo.error})` : ""} — set portable.home_ip in Config or choose Home or Away.`}
                  {mode !== "auto" && " Overriding detection."}
                </Typography>
              </Box>
            </Stack>
            <Stack direction={{ xs: "column", sm: "row" }} spacing={1} alignItems={{ sm: "center" }}>
              {!running ? (
                <Button
                  variant="contained"
                  size="large"
                  startIcon={<PlayArrowIcon />}
                  onClick={start}
                  disabled={!recipe || isHome === null}
                >
                  {isHome === true ? "Run at home" : isHome === false ? "Run here" : "Choose Home or Away"}
                </Button>
              ) : (
                <Button variant="outlined" color="warning" size="large" startIcon={<StopIcon />} onClick={stop}>
                  Cancel
                </Button>
              )}
              <Chip
                size="small"
                icon={<HomeIcon />}
                color={homeCount >= minHome ? "success" : "default"}
                variant="outlined"
                label={
                  homeCount >= minHome
                    ? `${homeCount} home runs on this device`
                    : `${homeCount}/${minHome} home runs on this device`
                }
              />
              {recipe && (
                <Typography variant="caption" color="text.secondary">
                  {recipe.resources.length} resources × {recipe.iterations} · about 45 s · ~
                  {Math.round(
                    ((recipe.resources.reduce((a, r) => a + r.bytes, 0) + (recipe.stream.bytes ?? 0)) *
                      recipe.iterations) /
                      1_000_000,
                  )}{" "}
                  MB
                </Typography>
              )}
            </Stack>
            {running && (
              <Box>
                <LinearProgress variant="determinate" value={Math.round((progress?.fraction ?? 0) * 100)} />
                <Typography variant="caption" color="text.secondary">
                  {progress?.stage ?? "starting"} — keep this page in the foreground
                </Typography>
              </Box>
            )}
            {error && <Alert severity="error">{error}</Alert>}
          </Stack>
        </CardContent>
      </Card>

      {result && recipe && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
              <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
                Result
              </Typography>
              <Chip size="small" label={fmtDateTime(result.created_at)} variant="outlined" />
              {result.is_home ? (
                <Chip size="small" color="primary" icon={<HomeIcon />} label="home" />
              ) : (
                <Chip size="small" icon={<FlightTakeoffIcon />} label={result.venue || "away"} />
              )}
              <Tooltip
                title={
                  result.home_detection === "ip"
                    ? `public address ${result.egress_ip ?? "?"} vs home ${result.home_ip ?? "?"}`
                    : "chosen by hand"
                }
              >
                <Chip size="small" variant="outlined" label={result.home_detection === "ip" ? "detected by address" : "set manually"} />
              </Tooltip>
              {result.settings_summary && <Chip size="small" variant="outlined" label={result.settings_summary} />}
            </Stack>
            <ScoreLine run={result} />
            <Provenance run={result} />
            <Divider sx={{ my: 1.5 }} />
            <MetricTable run={result} meta={recipe.metrics} />
            <OriginTable run={result} />
            <Failures run={result} />
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent>
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }} gutterBottom>
            Runs from this device
          </Typography>
          {!history.length ? (
            <Typography variant="body2" color="text.secondary">
              Nothing yet. Start at home: run it {minHome} times so there is a reference to compare against.
            </Typography>
          ) : (
            <List dense disablePadding>
              {history.map((r) => (
                <ListItemButton
                  key={r.id}
                  onClick={() => open(r.id)}
                  selected={result?.id === r.id}
                  sx={{ borderRadius: 1, alignItems: "flex-start" }}
                >
                  <ListItemText
                    primary={
                      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                        <Typography component="span" sx={{ fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>
                          {r.score != null ? Math.round(r.score) : "—"}
                        </Typography>
                        {r.is_home ? (
                          <Chip size="small" color="primary" icon={<HomeIcon />} label="home" />
                        ) : (
                          <Chip size="small" label={r.venue || "away"} />
                        )}
                        {r.instrument_version !== recipe?.instrument_version && (
                          <Chip size="small" variant="outlined" color="warning" label="older test version" />
                        )}
                      </Stack>
                    }
                    secondary={`${fmtDateTime(r.created_at)}${r.settings_summary ? ` · ${r.settings_summary}` : ""}`}
                  />
                  <IconButton
                    edge="end"
                    size="small"
                    aria-label="delete run"
                    onClick={(e) => {
                      e.stopPropagation();
                      remove(r.id);
                    }}
                  >
                    <DeleteOutlineIcon fontSize="small" />
                  </IconButton>
                </ListItemButton>
              ))}
            </List>
          )}
        </CardContent>
      </Card>
    </Box>
  );
}

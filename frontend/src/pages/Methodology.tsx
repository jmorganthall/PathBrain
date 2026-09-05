import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import FormControlLabel from "@mui/material/FormControlLabel";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import MenuItem from "@mui/material/MenuItem";
import CircularProgress from "@mui/material/CircularProgress";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import RestartAltIcon from "@mui/icons-material/RestartAlt";

import { api } from "../api/client";
import type {
  BrowserClient,
  BrowserConfig,
  MethodologyDetail,
  MethodologyMetric,
  MethodologySummary,
} from "../api/types";
import Loading from "../components/Loading";
import { Blurb, FoldCard, HelpTip } from "../components/Explain";
import StringListEditor from "../components/config/StringListEditor";
import { fmtDateTime } from "../utils/format";
import { vHttpUrl } from "../utils/validate";

// The client block as the Config page's browser section carries it (the same fields, with
// a 0×0 viewport standing for "Playwright's default").
function clientFromBrowser(b: BrowserConfig | undefined): BrowserClient {
  const w = Number(b?.viewport?.width ?? 0);
  const h = Number(b?.viewport?.height ?? 0);
  return {
    headless_mode: b?.headless_mode === "legacy" ? "legacy" : "new",
    hide_automation: b?.hide_automation ?? true,
    user_agent: b?.user_agent ?? "auto",
    viewport: w > 0 && h > 0 ? { width: w, height: h } : null,
    locale: b?.locale ?? "en-US",
    timezone_id: b?.timezone_id ?? "",
  };
}

const HEADLESS_MODES = [
  { value: "new", label: "New headless (the real browser)" },
  { value: "legacy", label: "Legacy headless shell" },
];

// The client fields of the "Sites measured" card: what every page is loaded AS. Shown and
// edited beside the URL lists because a site serves a headless shell, a phone-sized
// viewport or an automated client a different page than it serves a person — so the
// client is the other half of what a browser metric is a mean over, and changing it is a
// publish exactly like changing a site.
function ClientEditor({ value, onChange }: { value: BrowserClient; onChange: (c: BrowserClient) => void }) {
  const vp = value.viewport ?? { width: 0, height: 0 };
  return (
    <Box>
      <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
        Loaded as
        <HelpTip title="The client every page is loaded as. Sites hand a headless shell, a tiny viewport or an automated client a different page (or a challenge page) than they hand a person, so this is part of what the scores measure: changing any of it publishes a new version, like changing a site. Deliberately not a stealth arms race — a site that still challenges after this doesn't want automated loads, and the right answer is a different site." />
      </Typography>
      <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap alignItems="center">
        <TextField
          select
          size="small"
          label="Headless mode"
          value={value.headless_mode}
          onChange={(e) => onChange({ ...value, headless_mode: e.target.value })}
          sx={{ width: 250 }}
        >
          {HEADLESS_MODES.map((m) => (
            <MenuItem key={m.value} value={m.value}>
              {m.label}
            </MenuItem>
          ))}
        </TextField>
        <TextField
          size="small"
          label="User agent"
          value={value.user_agent}
          onChange={(e) => onChange({ ...value, user_agent: e.target.value })}
          helperText="auto = current desktop Chrome matching the bundled Chromium; empty = Playwright's default"
          sx={{ minWidth: 320, flexGrow: 1 }}
        />
      </Stack>
      <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap alignItems="center" sx={{ mt: 1.5 }}>
        <TextField
          size="small"
          type="number"
          label="Viewport width"
          value={vp.width || ""}
          onChange={(e) => onChange({ ...value, viewport: { width: parseInt(e.target.value, 10) || 0, height: vp.height } })}
          sx={{ width: 150 }}
        />
        <TextField
          size="small"
          type="number"
          label="Viewport height"
          value={vp.height || ""}
          onChange={(e) => onChange({ ...value, viewport: { width: vp.width, height: parseInt(e.target.value, 10) || 0 } })}
          sx={{ width: 150 }}
        />
        <TextField
          size="small"
          label="Locale"
          value={value.locale}
          onChange={(e) => onChange({ ...value, locale: e.target.value })}
          sx={{ width: 120 }}
        />
        <TextField
          size="small"
          label="Timezone (IANA)"
          value={value.timezone_id}
          onChange={(e) => onChange({ ...value, timezone_id: e.target.value })}
          placeholder="container's"
          sx={{ width: 200 }}
        />
        <FormControlLabel
          control={
            <Checkbox
              size="small"
              checked={value.hide_automation}
              onChange={(e) => onChange({ ...value, hide_automation: e.target.checked })}
            />
          }
          label="Clear navigator.webdriver"
        />
      </Stack>
    </Box>
  );
}

function fmtClient(c: BrowserClient | null | undefined): string {
  if (!c) return "client: whatever Config says";
  const vp = c.viewport ? `${c.viewport.width}×${c.viewport.height}` : "default viewport";
  const ua = c.user_agent === "auto" ? "desktop Chrome UA" : c.user_agent ? "custom UA" : "default UA";
  return `${c.headless_mode === "legacy" ? "legacy headless" : "new headless"}, ${vp}, ${ua}${c.locale ? `, ${c.locale}` : ""}`;
}

function fmtBound(v: number | null, unit: string): string {
  if (v == null) return "—";
  const n = Number.isInteger(v) ? v.toString() : v.toFixed(2);
  return `${n}${unit ? " " + unit : ""}`;
}

// The frozen metric table for one methodology, grouped by axis (display-only last).
function MetricTable({ metrics }: { metrics: MethodologyMetric[] }) {
  const axes = Array.from(new Set(metrics.map((m) => m.axis ?? "display")));
  return (
    <TableContainer sx={{ mt: 1 }}>
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Metric</TableCell>
            <TableCell>Axis</TableCell>
            <TableCell align="right">Weight</TableCell>
            <TableCell align="right">Best</TableCell>
            <TableCell align="right">Worst</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {axes.flatMap((axis) =>
            metrics
              .filter((m) => (m.axis ?? "display") === axis)
              .map((m) => (
                <TableRow key={m.key}>
                  <TableCell>
                    <Tooltip arrow title={m.description}>
                      <Box component="span" sx={{ cursor: "help" }}>
                        {m.label}
                        {m.required && (
                          <Chip
                            size="small"
                            label="required"
                            color="info"
                            variant="outlined"
                            sx={{ ml: 1, height: 18, fontSize: "0.6rem" }}
                          />
                        )}
                      </Box>
                    </Tooltip>
                  </TableCell>
                  <TableCell>
                    <Typography variant="caption" color="text.secondary">
                      {m.axis ?? "display-only"}
                    </Typography>
                  </TableCell>
                  <TableCell align="right">{m.axis ? m.weight : "—"}</TableCell>
                  <TableCell align="right">{fmtBound(m.best, m.unit)}</TableCell>
                  <TableCell align="right">{fmtBound(m.worst, m.unit)}</TableCell>
                </TableRow>
              )),
          )}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function VersionRow({ m }: { m: MethodologySummary }) {
  const recorded = m.metric_count > 0;
  return (
    <Box sx={{ py: 1 }}>
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
        <Typography variant="subtitle2">{m.version}</Typography>
        {m.is_current && <Chip size="small" color="success" label="current" />}
        {!recorded && (
          <Tooltip title="This version predates the methodology layer, so its full rubric wasn't recorded. Its scores survive; its definition can't be reconstructed.">
            <Chip size="small" variant="outlined" color="warning" label="definition not recorded" />
          </Tooltip>
        )}
        <Typography variant="caption" color="text.secondary">
          derivation {m.derivation_version} · {m.created_at ? fmtDateTime(m.created_at) : "—"}
        </Typography>
      </Stack>
      {recorded && (
        <Typography variant="caption" color="text.secondary">
          {m.scored_metric_count} scored metric(s) across {m.axes.map((a) => a.label).join(" + ")}
          {m.required_metrics.length > 0 && <> · requires {m.required_metrics.join(", ")}</>}
        </Typography>
      )}
      {m.notes && (
        <Blurb variant="caption" sx={{ mt: 0.25, mb: 0 }} moreLabel="Show" lessLabel="Hide" more={m.notes}>
          Version notes
        </Blurb>
      )}
    </Box>
  );
}

export default function Methodology() {
  const [current, setCurrent] = useState<MethodologyDetail | null>(null);
  const [versions, setVersions] = useState<MethodologySummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [regrading, setRegrading] = useState(false);
  const [rederiving, setRederiving] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  // A pending re-anchor proposal, deep-linked from the Settings-Impact saturation alert
  // (?reanchor=<metric>&best=<suggested>). The 'best' is editable before publishing.
  const [searchParams, setSearchParams] = useSearchParams();
  const reanchorKey = searchParams.get("reanchor");
  const suggestedBest = searchParams.get("best");
  // How many metrics the saturation alert flagged. When more than one, default the re-grade
  // OFF so the user can re-anchor them all first and re-grade once (a re-grade is heavy).
  const saturatedCount = Number(searchParams.get("saturated") ?? "1") || 1;
  const [proposalBest, setProposalBest] = useState("");
  const [regradeNow, setRegradeNow] = useState(true);
  const [publishing, setPublishing] = useState(false);
  // Which methodology scores runs "at present": the effective version, the version this build
  // ships as latest (code_default), and the config pin (null → follows the build). Lets the page
  // show + repair a stale pin (e.g. stuck on v10 after upgrading) without an API poke.
  const [pinState, setPinState] = useState<{ current: string; codeDefault: string; pinned: string | null } | null>(null);
  const [switching, setSwitching] = useState(false);
  // The site list: what the current version owns, or — for a version that measures whatever
  // Config says — the lists Config holds, offered here so publishing pins them to a version.
  const [siteBrowser, setSiteBrowser] = useState<string[]>([]);
  const [siteHttp, setSiteHttp] = useState<string[]>([]);
  // The client the pages are loaded as: what the version declares, or what Config holds.
  const [siteClient, setSiteClient] = useState<BrowserClient>(clientFromBrowser(undefined));
  const [siteRegrade, setSiteRegrade] = useState(true);
  const [publishingSites, setPublishingSites] = useState(false);

  useEffect(() => {
    if (suggestedBest != null) setProposalBest(suggestedBest);
  }, [suggestedBest]);
  useEffect(() => {
    setRegradeNow(saturatedCount <= 1);  // one metric → re-grade now; several → defer
  }, [saturatedCount, reanchorKey]);

  const load = useCallback(async () => {
    try {
      const [cur, list, cfg] = await Promise.all([
        api.methodologyCurrent(),
        api.methodologies(),
        api.config().catch(() => null),
      ]);
      setCurrent(cur);
      setVersions(list.methodologies);
      setSiteBrowser(cur.collection?.browser_urls ?? cfg?.browser.urls ?? []);
      setSiteHttp(cur.collection?.http_urls ?? cfg?.http.urls ?? []);
      setSiteClient(cur.collection?.client ?? clientFromBrowser(cfg?.browser));
      setPinState({
        current: list.current_version ?? cur.version,
        codeDefault: list.code_default ?? cur.version,
        pinned: list.pinned ?? null,
      });
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load methodologies");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleRegrade = useCallback(async () => {
    setRegrading(true);
    try {
      await api.regradeHistory();
      setToast("Re-grade started — track its progress in the jobs menu (top right) ↗");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the re-grade");
    } finally {
      setRegrading(false);
    }
  }, []);

  const handleRederive = useCallback(async () => {
    setRederiving(true);
    try {
      await api.rederiveHistory();
      setToast("Re-derive started — track its progress in the jobs menu (top right) ↗");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the re-derive");
    } finally {
      setRederiving(false);
    }
  }, []);

  const handleSetCurrent = useCallback(async (version: string | null) => {
    setSwitching(true);
    try {
      const res = await api.setCurrentMethodology(version);
      setToast(
        `Now scoring under ${res.version} — a re-grade started (jobs menu, top right ↗). If this version changed a formula, run “Re-derive” first.`,
      );
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not change the methodology");
    } finally {
      setSwitching(false);
    }
  }, [load]);

  const proposalMetric =
    reanchorKey && current
      ? current.definition.metrics.find((m) => m.key === reanchorKey) ?? null
      : null;

  const handlePublish = useCallback(async () => {
    if (!proposalMetric) return;
    const best = Number(proposalBest);
    if (!Number.isFinite(best)) {
      setError("Enter a numeric “best” value");
      return;
    }
    setPublishing(true);
    try {
      const res = await api.reanchorMetric(proposalMetric.key, best, regradeNow);
      setToast(
        regradeNow
          ? `Published ${res.version} and started a re-grade — track it in the jobs menu (top right) ↗`
          : `Published ${res.version} (no re-grade yet). Re-anchor any other saturated metrics, then click “Re-grade history under current” once to apply them all.`,
      );
      setSearchParams({}, { replace: true }); // clear the proposal from the URL
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not publish the re-anchor");
    } finally {
      setPublishing(false);
    }
  }, [proposalMetric, proposalBest, setSearchParams, load]);

  const handlePublishSites = useCallback(async () => {
    const browser = siteBrowser.map((u) => u.trim()).filter(Boolean);
    const http = siteHttp.map((u) => u.trim()).filter(Boolean);
    if (browser.length === 0) {
      setError("At least one browser URL is required");
      return;
    }
    if ([...browser, ...http].some((u) => vHttpUrl(u))) {
      setError("Fix the highlighted URLs before publishing");
      return;
    }
    setPublishingSites(true);
    try {
      const res = await api.publishSites(browser, http, siteRegrade, siteClient);
      if (!res.changed) {
        setToast(`The site list and client already match ${res.version} — nothing to publish.`);
      } else {
        const diff = [
          ...res.added.map((u) => `+${u}`),
          ...res.removed.map((u) => `−${u}`),
          ...(res.client_changes ?? []),
        ].join(", ");
        setToast(
          siteRegrade
            ? `Published ${res.version} (${diff || "reordered"}) and started a re-grade — earlier runs are now legacy. Track it in the jobs menu (top right) ↗`
            : `Published ${res.version} (${diff || "reordered"}). Run “Re-grade history under current” to quarantine earlier runs.`,
        );
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not publish the site list");
    } finally {
      setPublishingSites(false);
    }
  }, [siteBrowser, siteHttp, siteClient, siteRegrade, load]);

  if (loading) return <Loading label="Loading methodology…" />;

  const others = versions.filter((v) => v.version !== current?.version);

  return (
    <Box>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        spacing={1}
        sx={{ mb: 1 }}
      >
        <Typography variant="h4">Methodology</Typography>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1}>
          <Tooltip
            arrow
            title="Recompute every run's metrics from its stored raw. Use after a formula changes or a metric is added. Doesn't change the rubric."
          >
            <span>
              <Button
                variant="outlined"
                startIcon={rederiving ? <CircularProgress size={16} /> : <RestartAltIcon />}
                onClick={handleRederive}
                disabled={rederiving}
              >
                {rederiving ? "Re-deriving…" : "Re-derive history from raw"}
              </Button>
            </span>
          </Tooltip>
          <Tooltip
            arrow
            title="Re-score every run under the current methodology. Use after publishing new weights, thresholds, or a new crown. Re-derive first if the rubric needs a new measurement."
          >
            <span>
              <Button
                variant="outlined"
                startIcon={regrading ? <CircularProgress size={16} /> : <RestartAltIcon />}
                onClick={handleRegrade}
                disabled={regrading}
              >
                {regrading ? "Re-grading…" : "Re-grade history under current"}
              </Button>
            </span>
          </Tooltip>
        </Stack>
      </Stack>
      <Blurb
        variant="body2"
        sx={{ mb: 2 }}
        more={
          <>
            Raw data is the instrumented truth; the methodology is the interpretation applied to it.
            Changing a weight, threshold, or metric publishes a new version. Old scores keep the
            version they were measured under, and any run can be re-scored from its raw under the
            current one.
          </>
        }
      >
        How raw observations become a score, versioned.
      </Blurb>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {pinState && (() => {
        const stale = pinState.current !== pinState.codeDefault;
        return (
          <Card sx={{ mb: 2, ...(stale ? { border: 1, borderColor: "warning.main" } : {}) }}>
            <CardContent>
              <Stack
                direction={{ xs: "column", sm: "row" }}
                justifyContent="space-between"
                alignItems={{ xs: "flex-start", sm: "center" }}
                spacing={1}
              >
                <Box>
                  <Stack direction="row" spacing={1} alignItems="center">
                    <Typography variant="h6">Active methodology</Typography>
                    {pinState.pinned ? (
                      <Chip size="small" color="warning" variant="outlined" label="pinned" />
                    ) : (
                      <Chip size="small" color="success" variant="outlined" label="latest" />
                    )}
                  </Stack>
                  <Typography variant="body2" color="text.secondary">
                    Runs are scored “at present” under <b>{pinState.current}</b>
                    {pinState.pinned ? " (pinned in config)" : ""}. This build ships{" "}
                    <b>{pinState.codeDefault}</b> as the latest rubric.
                  </Typography>
                </Box>
                {stale && (
                  <Button
                    variant="contained"
                    color="warning"
                    onClick={() => handleSetCurrent(null)}
                    disabled={switching}
                  >
                    {switching ? "Switching…" : `Adopt latest (${pinState.codeDefault})`}
                  </Button>
                )}
              </Stack>
              <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mt: 1.5 }} alignItems={{ sm: "center" }}>
                <TextField
                  select
                  size="small"
                  label="Score under"
                  value={pinState.current}
                  onChange={(e) => handleSetCurrent(e.target.value)}
                  disabled={switching}
                  sx={{ minWidth: 300 }}
                >
                  {versions.map((v) => (
                    <MenuItem key={v.version} value={v.version}>
                      {v.version}
                      {v.version === pinState.codeDefault ? " · latest" : ""}
                    </MenuItem>
                  ))}
                </TextField>
                {pinState.pinned && (
                  <Button variant="outlined" onClick={() => handleSetCurrent(null)} disabled={switching}>
                    Clear pin
                  </Button>
                )}
              </Stack>
              {stale && (
                <Typography variant="caption" color="warning.main" sx={{ display: "block", mt: 1 }}>
                  Pinned to an older rubric. Adopt the latest, then re-grade to score history under it.
                </Typography>
              )}
            </CardContent>
          </Card>
        );
      })()}

      {reanchorKey && !proposalMetric && (
        <Alert severity="info" sx={{ mb: 2 }} onClose={() => setSearchParams({}, { replace: true })}>
          “{reanchorKey}” isn’t a scored metric in the current methodology, so it can’t be re-anchored.
        </Alert>
      )}

      {proposalMetric && (
        <Card sx={{ mb: 2, border: 1, borderColor: "warning.main" }}>
          <CardContent>
            <Typography variant="h6" gutterBottom>
              Proposed re-anchor — {proposalMetric.label}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
              <b>{proposalMetric.label}</b> scores ~100 for most profiles, so it can&apos;t rank them.
              Tightening “best” publishes a new version forked from <b>{current?.version}</b>.
              {saturatedCount > 1 && (
                <>
                  {" "}
                  <b>{saturatedCount} metrics are saturated</b>: re-anchor them all, then re-grade
                  once.
                </>
              )}
            </Typography>
            <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap" useFlexGap>
              <Typography variant="body2">
                Current best: <b>{fmtBound(proposalMetric.best, proposalMetric.unit)}</b>
              </Typography>
              <Typography variant="body2" color="text.secondary">
                →
              </Typography>
              <TextField
                label="New best"
                size="small"
                type="number"
                value={proposalBest}
                onChange={(e) => setProposalBest(e.target.value)}
                InputProps={{
                  endAdornment: proposalMetric.unit ? (
                    <Typography variant="caption" color="text.secondary">
                      {proposalMetric.unit}
                    </Typography>
                  ) : null,
                }}
                sx={{ width: 170 }}
              />
              <Tooltip
                arrow
                title="A re-grade re-scores all of history and can take a while. Leave it off to publish now and re-grade once after you've re-anchored every saturated metric."
              >
                <FormControlLabel
                  control={
                    <Checkbox
                      size="small"
                      checked={regradeNow}
                      onChange={(e) => setRegradeNow(e.target.checked)}
                    />
                  }
                  label="Re-grade now"
                />
              </Tooltip>
              <Button
                variant="contained"
                color="secondary"
                onClick={handlePublish}
                disabled={publishing}
                startIcon={publishing ? <CircularProgress size={16} /> : undefined}
              >
                {publishing
                  ? "Publishing…"
                  : regradeNow
                  ? "Publish new version & re-grade"
                  : "Publish new version"}
              </Button>
              <Button onClick={() => setSearchParams({}, { replace: true })} disabled={publishing}>
                Dismiss
              </Button>
            </Stack>
          </CardContent>
        </Card>
      )}

      {current && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
              <Typography variant="h6">{current.version}</Typography>
              <Chip size="small" color="success" label="current" />
              {current.axes.map((a) => (
                <Chip key={a.key} size="small" variant="outlined" label={a.label} />
              ))}
            </Stack>
            <Typography variant="caption" color="text.secondary">
              derivation {current.derivation_version}
              {current.created_at ? ` · recorded ${fmtDateTime(current.created_at)}` : ""}
            </Typography>
            {current.notes && (
              <Blurb variant="body2" sx={{ mt: 0.5, mb: 0.5 }} moreLabel="Notes" lessLabel="Hide notes" more={current.notes}>
                What changed in this version:
              </Blurb>
            )}
            <MetricTable metrics={current.definition.metrics} />
          </CardContent>
        </Card>
      )}

      {current && (
        <FoldCard
          title="Sites measured"
          summary={
            current.collection
              ? `${current.collection.browser_urls.length} browser page${current.collection.browser_urls.length === 1 ? "" : "s"}, ${current.collection.http_urls.length} HTTP URL${current.collection.http_urls.length === 1 ? "" : "s"}; ${fmtClient(current.collection.client)} — owned by this version.`
              : "This version measures whatever Config says. Publish a list here to pin it."
          }
        >
          <Typography variant="body2" sx={{ mb: 1.5 }}>
            Every browser score is an average over these pages, loaded as this client, so both
            are part of the methodology.
            <HelpTip title="Changing a site — or the client the pages are loaded as — publishes a new version. Runs measured against the old list or as the old client keep their scores under the old version and are quarantined as legacy under the new one — never pooled with the new measurements. Until fresh runs arrive, the heirs card, the challenger race and the duel ladder are ordered by the previous version's standings, so its winners get measured first; a duel already running when you publish re-seeds itself the same way." />
          </Typography>
          <Stack spacing={2}>
            <ClientEditor value={siteClient} onChange={setSiteClient} />
            <StringListEditor
              label="Browser pages"
              helperText="Real page loads in headless Chromium — the crown metrics come from these."
              items={siteBrowser}
              onChange={setSiteBrowser}
              validate={vHttpUrl}
              placeholder="https://example.com/"
              addLabel="Add page"
            />
            <StringListEditor
              label="HTTP URLs"
              helperText="TTFB and transfer speed probes."
              items={siteHttp}
              onChange={setSiteHttp}
              validate={vHttpUrl}
              placeholder="https://example.com/"
              addLabel="Add URL"
            />
            <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap" useFlexGap>
              <Tooltip
                arrow
                title="A re-grade re-scores all of history under the new version, which is what quarantines runs measured against the old list. Leave it off to publish now and re-grade once later."
              >
                <FormControlLabel
                  control={
                    <Checkbox size="small" checked={siteRegrade} onChange={(e) => setSiteRegrade(e.target.checked)} />
                  }
                  label="Re-grade now"
                />
              </Tooltip>
              <Button
                variant="contained"
                color="secondary"
                onClick={handlePublishSites}
                disabled={publishingSites}
                startIcon={publishingSites ? <CircularProgress size={16} /> : undefined}
              >
                {publishingSites ? "Publishing…" : "Publish sites + client as a new version"}
              </Button>
            </Stack>
          </Stack>
        </FoldCard>
      )}

      {others.length > 0 && (
        <FoldCard title={`Other versions (${others.length})`} summary="Earlier rubrics, kept frozen for the scores measured under them.">
            {others.map((m, i) => (
              <Box key={m.version}>
                {i > 0 && <Box sx={{ borderTop: "1px solid", borderColor: "divider" }} />}
                <VersionRow m={m} />
              </Box>
            ))}
        </FoldCard>
      )}

      <Snackbar
        open={toast != null}
        autoHideDuration={6000}
        onClose={() => setToast(null)}
        message={toast ?? ""}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      />
    </Box>
  );
}

import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Autocomplete from "@mui/material/Autocomplete";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import FormControlLabel from "@mui/material/FormControlLabel";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import InsightsIcon from "@mui/icons-material/Insights";
import NorthEastIcon from "@mui/icons-material/NorthEast";
import SouthEastIcon from "@mui/icons-material/SouthEast";
import RemoveIcon from "@mui/icons-material/Remove";
import ScienceIcon from "@mui/icons-material/Science";
import PublishIcon from "@mui/icons-material/Publish";

import LinearProgress from "@mui/material/LinearProgress";
import { Link as RouterLink } from "react-router-dom";
import Link from "@mui/material/Link";

import { api } from "../api/client";
import type {
  AiConfig,
  AiModel,
  AiRelationship,
  AiSuggestResult,
  AiSuggestion,
  FieldSensitivity,
  LeverSignature,
  ProfileTest,
  TopProfileSignature,
} from "../api/types";
import ApplyConfirmDialog, { type ApplyConfirm } from "../components/ApplyConfirmDialog";

// The settings→outcome relationship map. Two views: the deterministic per-field/metric Spearman
// correlations we computed and sent to the model (trustworthy, AI-independent), and the model's
// own interpreted relationships. This is the "figure out how the levers relate to the outcomes"
// step made explicit, rather than buried in the suggestion rationales.
function RelationshipsCard({
  sensitivity,
  relationships,
}: {
  sensitivity?: FieldSensitivity[];
  relationships?: AiRelationship[];
}) {
  const rows = sensitivity ?? [];
  const rels = relationships ?? [];
  if (rows.length === 0 && rels.length === 0) return null;

  const dirIcon = (d: FieldSensitivity["metric_direction"]) =>
    d === "increases" ? (
      <NorthEastIcon fontSize="inherit" />
    ) : d === "decreases" ? (
      <SouthEastIcon fontSize="inherit" />
    ) : (
      <RemoveIcon fontSize="inherit" />
    );

  return (
    <Card sx={{ mb: 2 }}>
      <CardContent>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
          <InsightsIcon fontSize="small" />
          <Typography variant="h6">Settings ↔ outcome relationships</Typography>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 820 }}>
          How each tunable lever moves each crown metric across your tested profiles — the
          interpretation step, computed deterministically (Spearman rank correlation) and sent to
          the model. These are <b>marginal</b> correlations (profiles vary several fields at once),
          so read them as directional evidence, not isolated cause and effect.
        </Typography>

        {rows.length > 0 ? (
          <Box sx={{ overflowX: "auto" }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Pipe</TableCell>
                  <TableCell>Lever</TableCell>
                  <TableCell>Outcome</TableCell>
                  <TableCell align="right">ρ</TableCell>
                  <TableCell align="center">n</TableCell>
                  <TableCell>Relationship</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {rows.map((r, i) => {
                  const strong = Math.abs(r.spearman ?? 0) >= 0.6;
                  return (
                    <TableRow key={i} hover>
                      <TableCell>{r.pipe}</TableCell>
                      <TableCell>{r.field_label}</TableCell>
                      <TableCell>{r.metric_label}</TableCell>
                      <TableCell
                        align="right"
                        sx={{ fontVariantNumeric: "tabular-nums", fontWeight: strong ? 700 : 400 }}
                      >
                        {r.spearman == null ? "—" : r.spearman.toFixed(2)}
                      </TableCell>
                      <TableCell align="center" sx={{ color: "text.secondary" }}>
                        {r.n}
                      </TableCell>
                      <TableCell>
                        <Tooltip title={r.summary}>
                          <Chip
                            size="small"
                            variant={r.effect === "none" ? "outlined" : "filled"}
                            color={
                              r.effect === "improves"
                                ? "success"
                                : r.effect === "worsens"
                                  ? "error"
                                  : "default"
                            }
                            icon={
                              <Box
                                component="span"
                                sx={{ display: "inline-flex", fontSize: 14, ml: 0.5 }}
                              >
                                {dirIcon(r.metric_direction)}
                              </Box>
                            }
                            label={
                              r.effect === "none"
                                ? "no clear trend"
                                : `metric ${r.metric_direction === "increases" ? "rises" : "falls"} · ${r.effect}`
                            }
                          />
                        </Tooltip>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </Box>
        ) : (
          <Alert severity="info" sx={{ mb: rels.length ? 2 : 0 }}>
            Not enough spread across profiles yet to compute field correlations — collect a few
            more profiles that vary the writable levers and they'll appear here.
          </Alert>
        )}

        {rels.length > 0 && (
          <Box sx={{ mt: rows.length ? 3 : 0 }}>
            <Typography variant="subtitle2" sx={{ mb: 1 }}>
              Model's interpretation
            </Typography>
            <Stack spacing={0.75}>
              {rels.map((r, i) => (
                <Stack
                  key={i}
                  direction="row"
                  spacing={1}
                  alignItems="center"
                  flexWrap="wrap"
                  useFlexGap
                >
                  <Chip
                    size="small"
                    color={
                      r.direction === "inverse"
                        ? "success"
                        : r.direction === "linear"
                          ? "error"
                          : "default"
                    }
                    variant={r.direction === "none" ? "outlined" : "filled"}
                    label={`${r.pipe ?? "?"} ${r.field ?? "?"} → ${r.metric ?? "?"}: ${r.direction ?? "?"}`}
                  />
                  {r.confidence && (
                    <Chip size="small" variant="outlined" label={String(r.confidence)} />
                  )}
                  {r.evidence && (
                    <Typography variant="body2" color="text.secondary">
                      {String(r.evidence)}
                    </Typography>
                  )}
                </Stack>
              ))}
            </Stack>
          </Box>
        )}
      </CardContent>
    </Card>
  );
}

// "What the top profiles share" — the deterministic top-vs-rest contrast. Answers the question
// the correlations can't: when every ρ is ~0 but some profiles still overperform, what settings
// do the winners have in common? Catches a shared sweet-spot value both extremes miss.
function TopProfilesCard({ signature }: { signature?: TopProfileSignature }) {
  if (!signature) return null;
  const levers = (signature.levers ?? []).filter((l) => l.pattern !== "none");
  const patternChip = (l: LeverSignature) => {
    if (l.pattern === "sweet_spot")
      return { color: "info" as const, label: "sweet spot" };
    if (l.pattern === "higher") return { color: "success" as const, label: "runs higher" };
    return { color: "warning" as const, label: "runs lower" };
  };
  const fmt = (n: number) => (Math.abs(n) >= 100 ? Math.round(n) : Math.round(n * 100) / 100);

  return (
    <Card sx={{ mb: 2 }}>
      <CardContent>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
          <InsightsIcon fontSize="small" />
          <Typography variant="h6">What the top profiles share</Typography>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 820 }}>
          The settings the <b>top-Overall profiles</b> have in common, vs the rest of the field.
          This catches what the correlations can't: a lever can show no monotonic trend yet the
          winners still cluster on a specific value (a <b>sweet spot</b> both extremes miss) or run
          it systematically higher/lower.
          {signature.available === false && signature.reason ? ` (${signature.reason})` : ""}
        </Typography>

        {signature.available === false || levers.length === 0 ? (
          <Alert severity="info">
            No lever stood out among the top profiles yet — they don't share a distinctive value
            beyond what the whole field runs. Collect more profiles that vary the levers, or the
            edge may be an interaction of several at once.
          </Alert>
        ) : (
          <Box sx={{ overflowX: "auto" }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Pipe</TableCell>
                  <TableCell>Lever</TableCell>
                  <TableCell>Top profiles run</TableCell>
                  <TableCell>Whole field</TableCell>
                  <TableCell>Pattern</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {levers.map((l, i) => {
                  const chip = patternChip(l);
                  return (
                    <TableRow key={i} hover>
                      <TableCell>{l.pipe}</TableCell>
                      <TableCell>{l.field_label}</TableCell>
                      <TableCell sx={{ fontVariantNumeric: "tabular-nums" }}>
                        <b>{fmt(l.top_value)}</b>{" "}
                        <Typography component="span" variant="caption" color="text.secondary">
                          ({fmt(l.top_range[0])}–{fmt(l.top_range[1])})
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ fontVariantNumeric: "tabular-nums", color: "text.secondary" }}>
                        {fmt(l.field_range[0])}–{fmt(l.field_range[1])}
                      </TableCell>
                      <TableCell>
                        <Tooltip title={l.summary}>
                          <Chip size="small" color={chip.color} label={chip.label} />
                        </Tooltip>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </Box>
        )}
      </CardContent>
    </Card>
  );
}

export default function AI() {
  const [cfg, setCfg] = useState<AiConfig | null>(null);
  const [keyInput, setKeyInput] = useState("");
  const [model, setModel] = useState("");
  const [prompt, setPrompt] = useState("");
  const [runsPerProfile, setRunsPerProfile] = useState(50);
  const [profileLimit, setProfileLimit] = useState(25);

  const [models, setModels] = useState<AiModel[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [result, setResult] = useState<AiSuggestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  // Streaming (SSE) state — the model's live reasoning trace + answer as they arrive.
  const [streamMode, setStreamMode] = useState(true);
  const [streamReasoning, setStreamReasoning] = useState("");
  const [streamContent, setStreamContent] = useState("");

  useEffect(() => {
    api
      .aiConfig()
      .then((c) => {
        setCfg(c);
        setModel(c.model);
        setPrompt(c.prompt);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load AI settings"));
  }, []);

  const loadModels = useCallback(async () => {
    setModelsLoading(true);
    setError(null);
    try {
      setModels((await api.aiModels()).models);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load models (is the API key set?)");
    } finally {
      setModelsLoading(false);
    }
  }, []);

  const save = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      const c = await api.aiSaveConfig({
        api_key: keyInput || undefined, // blank leaves the stored key untouched
        model,
        prompt,
      });
      setCfg(c);
      setKeyInput("");
      setToast("Saved AI settings");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save AI settings");
    } finally {
      setSaving(false);
    }
  }, [keyInput, model, prompt]);

  const clearKey = useCallback(async () => {
    try {
      setCfg(await api.aiClearKey());
      setToast("Cleared the stored API key");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to clear key");
    }
  }, []);

  const suggest = useCallback(async () => {
    setSuggesting(true);
    setError(null);
    setResult(null);
    setStreamReasoning("");
    setStreamContent("");
    const body = {
      model,
      prompt,
      runs_per_profile: runsPerProfile,
      profile_limit: profileLimit || null,
    };
    try {
      if (!streamMode) {
        setResult(await api.aiSuggest(body));
        return;
      }
      // Stream: accumulate reasoning + content deltas live, materialize the result on `done`.
      let reasoning = "";
      let content = "";
      let meta: {
        profiles_sent: number | null;
        payload_bytes: number;
        field_sensitivity?: FieldSensitivity[];
        top_profile_signature?: TopProfileSignature;
      } = {
        profiles_sent: null,
        payload_bytes: 0,
      };
      await api.aiSuggestStream(body, (evt) => {
        switch (evt.type) {
          case "meta":
            meta = {
              profiles_sent: evt.profiles_sent,
              payload_bytes: evt.payload_bytes,
              field_sensitivity: evt.field_sensitivity,
              top_profile_signature: evt.top_profile_signature,
            };
            break;
          case "reasoning":
            reasoning += evt.delta;
            setStreamReasoning(reasoning);
            break;
          case "content":
            content += evt.delta;
            setStreamContent(content);
            break;
          case "error":
            setError(evt.error);
            break;
          case "done":
            setResult({
              model: evt.model,
              raw: evt.raw,
              suggestions: evt.suggestions,
              relationships: evt.relationships,
              field_sensitivity: meta.field_sensitivity,
              top_profile_signature: meta.top_profile_signature,
              usage: evt.usage,
              profiles_sent: meta.profiles_sent,
              payload_bytes: meta.payload_bytes,
            });
            break;
        }
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Suggestion request failed");
    } finally {
      setSuggesting(false);
    }
  }, [model, prompt, runsPerProfile, profileLimit, streamMode]);

  const copy = useCallback(async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setToast(`Copied ${label}`);
    } catch {
      setToast("Clipboard unavailable");
    }
  }, []);

  const [testingIdx, setTestingIdx] = useState<number | null>(null);
  const [activeTest, setActiveTest] = useState<ProfileTest | null>(null);
  const testSuggestion = useCallback(
    async (s: AiSuggestion, idx: number) => {
      setTestingIdx(idx);
      setError(null);
      try {
        const r = await api.testSettings({
          settings: s.settings ?? s,
          label: `AI: ${String(s.rationale ?? "suggestion").slice(0, 60)}`,
        });
        setToast(`Applied to the firewall — testing to minimum (${r.iterations} iterations).`);
        // Show the live step-by-step readout below (snapshot → apply → verify → benchmark → restore).
        try {
          const cur = await api.profileTestCurrent();
          setActiveTest(cur.test);
        } catch {
          /* the panel will pick it up on the next poll */
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not start the test");
      } finally {
        setTestingIdx(null);
      }
    },
    [],
  );

  // Poll the live profile test while one is running, so the user watches it step through
  // (and sees any failure inline) rather than the job blinking out of the top bar.
  const testActive = activeTest?.status === "running" || activeTest?.status === "pending";
  useEffect(() => {
    if (!testActive) return;
    let cancelled = false;
    const id = setInterval(async () => {
      try {
        const cur = await api.profileTestCurrent();
        if (!cancelled) setActiveTest(cur.test);
      } catch {
        /* transient; keep the last snapshot */
      }
    }, 2000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [testActive]);

  // "Apply" — write a suggestion to the firewall PERMANENTLY, reusing the same confirm-diff
  // dialog as Settings Impact's "Apply this profile" (preview → confirm → commit + benchmark).
  const [previewingIdx, setPreviewingIdx] = useState<number | null>(null);
  const [applyConfirm, setApplyConfirm] = useState<ApplyConfirm | null>(null);
  const [applying, setApplying] = useState(false);
  const [applyRunBenchmark, setApplyRunBenchmark] = useState(true);

  const previewApply = useCallback(async (s: AiSuggestion, idx: number) => {
    setPreviewingIdx(idx);
    setError(null);
    const settings = s.settings ?? s;
    const label = `AI: ${String(s.rationale ?? "suggestion").slice(0, 60)}`;
    try {
      const r = await api.applySettings({ settings, label, preview: true });
      setApplyConfirm({
        fingerprint: r.fingerprint,
        settings,
        label: r.label || label,
        changes: r.changes ?? [],
        warnings: r.warnings ?? [],
        alreadyApplied: r.already_applied,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not preview the changes");
    } finally {
      setPreviewingIdx(null);
    }
  }, []);

  const confirmApply = useCallback(async () => {
    if (!applyConfirm) return;
    setApplying(true);
    setError(null);
    try {
      const r = await api.applySettings({
        settings: applyConfirm.settings,
        label: applyConfirm.label,
        preview: false,
        run_benchmark: applyRunBenchmark,
      });
      const base = `Applied ${r.applied?.length ?? 0} change(s) to the firewall — now on ${r.label}`;
      setToast(applyRunBenchmark ? `${base} · benchmarking now` : base);
      setApplyConfirm(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to apply to the firewall");
    } finally {
      setApplying(false);
    }
  }, [applyConfirm, applyRunBenchmark]);

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 1 }}>
        AI
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 860 }}>
        Send your measured profiles (full settings + scoring data) to an LLM via <b>OpenRouter</b> and get
        back proposed shaper profiles that haven't been tested yet — ranked by the model's estimate of the
        chance each beats your current crown. Configure a key and model, tweak the prompt if you like, then
        ask for suggestions. Each one has a <b>Test to minimum</b> button (apply → benchmark to the
        confidence minimum → restore your baseline) and an <b>Apply</b> button that writes it to the
        firewall <b>permanently</b> — the same confirm-diff dialog as Settings Impact, showing the exact
        field changes before you commit. Only writable fields are touched, so a suggestion is always
        reachable, and nothing is applied without your click.
      </Typography>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* Configuration */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            OpenRouter
          </Typography>
          <Stack spacing={2}>
            <Stack direction={{ xs: "column", sm: "row" }} spacing={2} alignItems={{ sm: "center" }}>
              <TextField
                label={cfg?.configured ? `API key (stored ${cfg.key_hint})` : "API key"}
                type="password"
                size="small"
                value={keyInput}
                onChange={(e) => setKeyInput(e.target.value)}
                placeholder={cfg?.configured ? "Leave blank to keep the stored key" : "sk-or-…"}
                sx={{ minWidth: 320, flex: 1 }}
                autoComplete="off"
              />
              {cfg?.configured && (
                <Button color="inherit" onClick={clearKey}>
                  Clear key
                </Button>
              )}
            </Stack>

            <Stack direction={{ xs: "column", sm: "row" }} spacing={2} alignItems={{ sm: "center" }}>
              <Autocomplete
                freeSolo
                options={models.map((m) => m.id)}
                value={model}
                onInputChange={(_e, v) => setModel(v)}
                sx={{ minWidth: 340, flex: 1 }}
                renderInput={(params) => (
                  <TextField
                    {...params}
                    label="Model"
                    size="small"
                    placeholder="e.g. anthropic/claude-sonnet-4"
                    helperText="OpenRouter model id — type it, or load the catalog to search"
                  />
                )}
              />
              <Button
                variant="outlined"
                onClick={loadModels}
                disabled={modelsLoading}
                startIcon={modelsLoading ? <CircularProgress size={16} color="inherit" /> : undefined}
              >
                {models.length ? `Reload models (${models.length})` : "Load models"}
              </Button>
            </Stack>

            <TextField
              label="Prompt (instructions sent before the data)"
              multiline
              minRows={6}
              maxRows={20}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              fullWidth
              InputProps={{ sx: { fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 13 } }}
            />
            <Stack direction="row" spacing={1}>
              <Button variant="contained" onClick={save} disabled={saving}>
                {saving ? "Saving…" : "Save settings"}
              </Button>
              {cfg && (
                <Button color="inherit" onClick={() => setPrompt(cfg.default_prompt)}>
                  Reset prompt to default
                </Button>
              )}
            </Stack>
          </Stack>
        </CardContent>
      </Card>

      {/* Suggest */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap" useFlexGap>
            <TextField
              label="Top profiles"
              type="number"
              size="small"
              value={profileLimit}
              onChange={(e) => setProfileLimit(parseInt(e.target.value || "0", 10))}
              inputProps={{ min: 1, max: 1000 }}
              sx={{ width: 150 }}
              helperText="Best N by Overall"
            />
            <TextField
              label="Runs per profile"
              type="number"
              size="small"
              value={runsPerProfile}
              onChange={(e) => setRunsPerProfile(parseInt(e.target.value || "0", 10))}
              inputProps={{ min: 1, max: 1000 }}
              sx={{ width: 160 }}
              helperText="Data sent per profile"
            />
            <Button
              variant="contained"
              onClick={suggest}
              disabled={suggesting || !cfg?.configured}
              startIcon={suggesting ? <CircularProgress size={16} color="inherit" /> : <AutoAwesomeIcon />}
            >
              {suggesting ? "Thinking…" : "Suggest profiles"}
            </Button>
            <FormControlLabel
              control={
                <Switch
                  checked={streamMode}
                  onChange={(e) => setStreamMode(e.target.checked)}
                  disabled={suggesting}
                />
              }
              label="Stream"
            />
            {!cfg?.configured && (
              <Typography variant="caption" color="text.secondary">
                Add an API key and model first.
              </Typography>
            )}
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
            Streaming shows the model's reasoning + answer live and keeps a long request from
            timing out.
          </Typography>
        </CardContent>
      </Card>

      {suggesting && streamMode && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
              <CircularProgress size={16} />
              <Typography variant="h6">Streaming…</Typography>
            </Stack>
            {streamReasoning && (
              <>
                <Typography variant="overline" color="text.secondary">
                  Reasoning
                </Typography>
                <Box
                  sx={{
                    maxHeight: 200,
                    overflow: "auto",
                    p: 1,
                    mb: 1,
                    borderRadius: 1,
                    bgcolor: "action.hover",
                    fontFamily: "monospace",
                    fontSize: 12,
                    whiteSpace: "pre-wrap",
                    color: "text.secondary",
                  }}
                >
                  {streamReasoning}
                </Box>
              </>
            )}
            {streamContent && (
              <>
                <Typography variant="overline" color="text.secondary">
                  Answer
                </Typography>
                <Box
                  sx={{
                    maxHeight: 260,
                    overflow: "auto",
                    p: 1,
                    borderRadius: 1,
                    bgcolor: "action.hover",
                    fontFamily: "monospace",
                    fontSize: 12,
                    whiteSpace: "pre-wrap",
                  }}
                >
                  {streamContent}
                </Box>
              </>
            )}
            {!streamReasoning && !streamContent && (
              <Typography variant="body2" color="text.secondary">
                Waiting for the model's first tokens…
              </Typography>
            )}
          </CardContent>
        </Card>
      )}

      {activeTest && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }} flexWrap="wrap">
              <ScienceIcon fontSize="small" />
              <Typography variant="h6">Live test</Typography>
              <Chip
                size="small"
                label={activeTest.status}
                color={
                  activeTest.status === "failed"
                    ? "error"
                    : activeTest.status === "complete"
                      ? "success"
                      : "primary"
                }
              />
              {activeTest.label && (
                <Typography variant="body2" color="text.secondary">
                  {activeTest.label}
                </Typography>
              )}
            </Stack>
            {testActive && <LinearProgress sx={{ mb: 1.5 }} />}
            {/* Step-by-step readout so it's clear the firewall is being written and measured. */}
            <Typography variant="body2" sx={{ mb: 1 }}>
              {activeTest.stage ||
                (activeTest.status === "pending" ? "Queued…" : "Working…")}
            </Typography>
            {activeTest.status === "failed" && activeTest.error && (
              <Alert severity="error" sx={{ mb: 1 }}>
                {activeTest.error}
              </Alert>
            )}
            {activeTest.status === "complete" && (
              <Alert severity="success" sx={{ mb: 1 }}>
                Benchmark complete and your original settings were restored.
                {activeTest.run_id != null && (
                  <>
                    {" "}
                    <Link component={RouterLink} to={`/results/${activeTest.run_id}`}>
                      View the run
                    </Link>
                    .
                  </>
                )}
              </Alert>
            )}
            <Typography variant="caption" color="text.secondary">
              Applies the suggestion to the live firewall (writable fields only), benchmarks{" "}
              {activeTest.iterations} iteration(s), then restores your baseline. Also shown in the
              jobs menu (top-right) and on{" "}
              <Link component={RouterLink} to="/settings">
                Settings Impact
              </Link>
              .
            </Typography>
          </CardContent>
        </Card>
      )}

      {result && (
        <RelationshipsCard
          sensitivity={result.field_sensitivity}
          relationships={result.relationships}
        />
      )}

      {result && <TopProfilesCard signature={result.top_profile_signature} />}

      {result && (
        <Card>
          <CardContent>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }} flexWrap="wrap">
              <Typography variant="h6">
                {result.suggestions.length} suggestion{result.suggestions.length === 1 ? "" : "s"}
              </Typography>
              <Chip size="small" label={result.model} />
              {result.profiles_sent != null && (
                <Chip size="small" variant="outlined" label={`${result.profiles_sent} profiles sent`} />
              )}
              {result.payload_bytes != null && (
                <Chip
                  size="small"
                  variant="outlined"
                  label={`${Math.round(result.payload_bytes / 1024)} KB payload`}
                />
              )}
              {typeof result.usage?.total_tokens === "number" && (
                <Chip size="small" variant="outlined" label={`${result.usage.total_tokens} tokens`} />
              )}
            </Stack>

            {result.suggestions.length === 0 && (
              <Alert severity="warning" sx={{ mb: 2 }}>
                Couldn't parse structured suggestions from the reply — see the raw response below.
              </Alert>
            )}

            <Stack spacing={1.5}>
              {result.suggestions.map((s, i) => {
                const likelihood =
                  typeof s.displacement_likelihood === "number"
                    ? (s.displacement_likelihood as number)
                    : null;
                return (
                  <Box key={i} sx={{ p: 1.5, border: 1, borderColor: "divider", borderRadius: 1 }}>
                    <Stack
                      direction="row"
                      spacing={1}
                      alignItems="center"
                      sx={{ mb: 1 }}
                      flexWrap="wrap"
                      useFlexGap
                    >
                      <Chip size="small" label={`#${i + 1}`} />
                      {likelihood != null && (
                        <Chip
                          size="small"
                          color={likelihood >= 60 ? "success" : likelihood >= 30 ? "warning" : "default"}
                          label={`${Math.round(likelihood)}% chance to beat crown`}
                        />
                      )}
                      <Box sx={{ flex: 1 }} />
                      <Button
                        size="small"
                        variant="contained"
                        startIcon={
                          testingIdx === i ? <CircularProgress size={14} color="inherit" /> : <ScienceIcon />
                        }
                        disabled={testingIdx != null || !s.settings}
                        onClick={() => testSuggestion(s, i)}
                      >
                        Test to minimum
                      </Button>
                      <Button
                        size="small"
                        color="warning"
                        startIcon={
                          previewingIdx === i ? (
                            <CircularProgress size={14} color="inherit" />
                          ) : (
                            <PublishIcon />
                          )
                        }
                        disabled={previewingIdx != null || !s.settings}
                        onClick={() => previewApply(s, i)}
                      >
                        Apply
                      </Button>
                      <Button
                        size="small"
                        startIcon={<ContentCopyIcon />}
                        onClick={() => copy(JSON.stringify(s.settings ?? s, null, 2), "settings")}
                      >
                        Copy
                      </Button>
                    </Stack>
                    {s.rationale && (
                      <Typography variant="body2" sx={{ mb: 1 }}>
                        {String(s.rationale)}
                      </Typography>
                    )}
                    <Box
                      component="pre"
                      sx={{
                        m: 0,
                        p: 1,
                        fontSize: 12,
                        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                        bgcolor: "background.default",
                        borderRadius: 1,
                        overflow: "auto",
                        whiteSpace: "pre-wrap",
                      }}
                    >
                      {JSON.stringify(s.settings ?? s, null, 2)}
                    </Box>
                  </Box>
                );
              })}
            </Stack>

            <Divider sx={{ my: 2 }} />
            <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 0.5 }}>
              <Typography variant="subtitle2" color="text.secondary">
                Raw response
              </Typography>
              <Button size="small" startIcon={<ContentCopyIcon />} onClick={() => copy(result.raw, "raw response")}>
                Copy
              </Button>
            </Stack>
            <Box
              component="pre"
              sx={{
                m: 0,
                p: 1.5,
                maxHeight: "40vh",
                overflow: "auto",
                fontSize: 12,
                fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                bgcolor: "background.default",
                borderRadius: 1,
                whiteSpace: "pre-wrap",
              }}
            >
              {result.raw}
            </Box>
          </CardContent>
        </Card>
      )}

      <ApplyConfirmDialog
        confirm={applyConfirm}
        applying={applying}
        runBenchmark={applyRunBenchmark}
        onRunBenchmarkChange={setApplyRunBenchmark}
        onCancel={() => setApplyConfirm(null)}
        onConfirm={confirmApply}
        title="Apply suggestion to firewall"
      />

      <Snackbar
        open={toast != null}
        autoHideDuration={2500}
        onClose={() => setToast(null)}
        message={toast ?? ""}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      />
    </Box>
  );
}

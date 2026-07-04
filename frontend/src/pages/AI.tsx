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
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import ScienceIcon from "@mui/icons-material/Science";

import { api } from "../api/client";
import type { AiConfig, AiModel, AiSuggestResult, AiSuggestion } from "../api/types";

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
    try {
      setResult(
        await api.aiSuggest({
          model,
          prompt,
          runs_per_profile: runsPerProfile,
          profile_limit: profileLimit || null,
        }),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Suggestion request failed");
    } finally {
      setSuggesting(false);
    }
  }, [model, prompt, runsPerProfile, profileLimit]);

  const copy = useCallback(async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setToast(`Copied ${label}`);
    } catch {
      setToast("Clipboard unavailable");
    }
  }, []);

  const [testingIdx, setTestingIdx] = useState<number | null>(null);
  const testSuggestion = useCallback(
    async (s: AiSuggestion, idx: number) => {
      setTestingIdx(idx);
      setError(null);
      try {
        const r = await api.testSettings({
          settings: s.settings ?? s,
          label: `AI: ${String(s.rationale ?? "suggestion").slice(0, 60)}`,
        });
        setToast(
          `Applied to the firewall — testing to minimum (${r.iterations} iterations), ` +
            `then restoring. Track it in the jobs menu (top-right) or on Settings Impact.`,
        );
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not start the test");
      } finally {
        setTestingIdx(null);
      }
    },
    [],
  );

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 1 }}>
        AI
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 860 }}>
        Send your measured profiles (full settings + scoring data) to an LLM via <b>OpenRouter</b> and get
        back proposed shaper profiles that haven't been tested yet — ranked by the model's estimate of the
        chance each beats your current crown. Configure a key and model, tweak the prompt if you like, then
        ask for suggestions. Each one has a <b>Test to minimum</b> button that applies it to the live
        firewall (writable fields only), benchmarks it to the confidence minimum, and restores your
        baseline — flowing the AI's ideas straight into the measurement loop. Nothing is applied without
        that click.
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
            {!cfg?.configured && (
              <Typography variant="caption" color="text.secondary">
                Add an API key and model first.
              </Typography>
            )}
          </Stack>
        </CardContent>
      </Card>

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

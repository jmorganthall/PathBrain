import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import ExtensionIcon from "@mui/icons-material/Extension";
import SystemUpdateAltIcon from "@mui/icons-material/SystemUpdateAlt";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import ErrorIcon from "@mui/icons-material/Error";

import { api } from "../api/client";
import type { PluginInfo, UpdateConfig, UpdateConnectionTest } from "../api/types";
import Loading from "../components/Loading";

// The Watchtower self-update integration: a card on the Plugins page that shows whether the
// integration is configured (URL + token) and offers a side-effect-free "Test connection" that
// probes reachability WITHOUT triggering an update (Watchtower's only endpoint performs the update,
// so the test hits the API root, not /v1/update).
function WatchtowerIntegration() {
  const [cfg, setCfg] = useState<UpdateConfig | null>(null);
  const [test, setTest] = useState<UpdateConnectionTest | null>(null);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .selfUpdateConfig()
      .then((c) => alive && setCfg(c))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const runTest = () => {
    setTesting(true);
    setTest(null);
    api
      .testUpdateConnection()
      .then((r) => {
        setTest(r);
        setCfg({ configured: r.configured, url: r.url, token_set: r.token_set });
      })
      .catch((e) =>
        setTest({
          configured: cfg?.configured ?? false,
          url: cfg?.url ?? null,
          token_set: cfg?.token_set ?? false,
          reachable: false,
          status: "unreachable",
          detail: e instanceof Error ? e.message : "Test failed.",
        }),
      )
      .finally(() => setTesting(false));
  };

  const configured = cfg?.configured ?? false;
  return (
    <Card>
      <CardContent>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
          <SystemUpdateAltIcon color="primary" fontSize="small" />
          <Typography variant="subtitle1" sx={{ fontWeight: 600, flexGrow: 1 }}>
            Watchtower (self-update)
          </Typography>
          <Chip
            size="small"
            label={configured ? "Configured" : "Not configured"}
            color={configured ? "success" : "default"}
            variant={configured ? "filled" : "outlined"}
          />
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          One-click updates via Watchtower's HTTP API. When configured, the top-bar "Update
          available" chip gains an "Update now" button that pulls the newer image and recreates this
          container.
        </Typography>

        {configured ? (
          <Stack spacing={0.5} sx={{ mb: 1.5 }}>
            <Typography variant="caption" color="text.secondary">
              URL:{" "}
              <Box component="span" sx={{ fontFamily: "monospace" }}>
                {cfg?.url}
              </Box>
            </Typography>
            <Typography variant="caption" color="text.secondary">
              Token: {cfg?.token_set ? "set" : "not set"}
            </Typography>
          </Stack>
        ) : (
          <Alert severity="info" sx={{ mb: 1.5 }}>
            Set <code>PATHBRAIN_WATCHTOWER_URL</code> (and <code>PATHBRAIN_WATCHTOWER_TOKEN</code>) in
            your environment / compose file to enable one-click updates.
          </Alert>
        )}

        <Stack direction="row" spacing={1} alignItems="center">
          <Tooltip title="Probe Watchtower's reachability. Does NOT trigger an update.">
            <span>
              <Button
                size="small"
                variant="outlined"
                onClick={runTest}
                disabled={!configured || testing}
                startIcon={testing ? <CircularProgress size={14} /> : undefined}
              >
                {testing ? "Testing…" : "Test connection"}
              </Button>
            </span>
          </Tooltip>
          {test && (
            <Stack direction="row" spacing={0.5} alignItems="center">
              {test.status === "ok" ? (
                <CheckCircleIcon color="success" fontSize="small" />
              ) : (
                <ErrorIcon color="error" fontSize="small" />
              )}
              <Typography variant="caption" color={test.status === "ok" ? "success.main" : "error.main"}>
                {test.status === "ok" ? "Reachable" : test.status === "unreachable" ? "Unreachable" : "Not configured"}
              </Typography>
            </Stack>
          )}
        </Stack>
        {test?.detail && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
            {test.detail}
          </Typography>
        )}
      </CardContent>
    </Card>
  );
}

export default function Plugins() {
  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        setPlugins(await api.plugins());
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load plugins");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  if (loading) return <Loading label="Loading plugins…" />;

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 3 }}>
        Plugins
      </Typography>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}

      <Box
        sx={{
          display: "grid",
          gap: 2,
          gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr", lg: "repeat(3, 1fr)" },
          mb: 3,
        }}
      >
        {plugins.map((p) => (
          <Card key={p.name}>
            <CardContent>
              <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
                <ExtensionIcon color="primary" fontSize="small" />
                <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
                  {p.name}
                </Typography>
              </Stack>
              <Typography variant="body2" color="text.secondary">
                {p.description}
              </Typography>
            </CardContent>
          </Card>
        ))}
      </Box>

      <Typography variant="h5" sx={{ mb: 2 }}>
        Integrations
      </Typography>
      <Box
        sx={{
          display: "grid",
          gap: 2,
          gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr", lg: "repeat(3, 1fr)" },
        }}
      >
        <WatchtowerIntegration />
      </Box>
    </Box>
  );
}

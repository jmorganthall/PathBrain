import { useCallback, useEffect, useState } from "react";
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
import type {
  PluginInfo,
  UpdateAttempt,
  UpdateConfig,
  UpdateConnectionTest,
  UpdateLog,
} from "../api/types";
import Loading from "../components/Loading";
import { fmtDateTime } from "../utils/format";

// How each attempt's *verdict* reads. The verdict is deliberately separate from the call's
// outcome: "Watchtower accepted the request" was always the thing we could observe, and it is
// NOT the thing the user is asking about. Only a changed build proves an update happened.
const VERDICTS: Record<string, { label: string; color: "success" | "warning" | "error" | "info"; what: string }> = {
  confirmed: {
    label: "Updated",
    color: "success",
    what: "The running build changed after this attempt — the update took effect.",
  },
  no_change: {
    label: "No change",
    color: "warning",
    what:
      "Watchtower accepted the request but the build never changed. Usually: this container is " +
      "outside Watchtower's scope, the image was already current, or Watchtower can't reach the registry.",
  },
  failed: {
    label: "Failed",
    color: "error",
    what: "The request never got through — nothing was updated.",
  },
  pending: {
    label: "Pending",
    color: "info",
    what:
      "Waiting to see whether the build changes. A successful update recreates this container, so " +
      "the verdict is written at the next startup.",
  },
};

function AttemptRow({ attempt }: { attempt: UpdateAttempt }) {
  const v = VERDICTS[attempt.verdict] ?? VERDICTS.pending;
  const call = [
    attempt.outcome,
    attempt.http_status ? `HTTP ${attempt.http_status}` : null,
    attempt.elapsed_ms != null ? `${attempt.elapsed_ms} ms` : null,
    attempt.token_sent ? "token sent" : "no token",
  ]
    .filter(Boolean)
    .join(" · ");
  const builds = [
    attempt.git_sha_before ? `from ${attempt.git_sha_before.slice(0, 7)}` : null,
    attempt.git_sha_after ? `to ${attempt.git_sha_after.slice(0, 7)}` : null,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <Box sx={{ py: 0.75, borderTop: 1, borderColor: "divider" }}>
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
        <Tooltip title={v.what}>
          <Chip size="small" label={v.label} color={v.color} sx={{ height: 20 }} />
        </Tooltip>
        <Typography variant="caption" color="text.secondary">
          {fmtDateTime(attempt.created_at)}
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ fontFamily: "monospace" }}>
          {call}
          {builds ? ` · ${builds}` : ""}
        </Typography>
      </Stack>
      {(attempt.detail || attempt.error || attempt.response_body) && (
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.25 }}>
          {attempt.detail || attempt.error || attempt.response_body}
        </Typography>
      )}
    </Box>
  );
}

// The Watchtower self-update integration: a card on the Plugins page that shows whether the
// integration is configured (URL + token) and offers a side-effect-free "Test connection" that
// probes reachability WITHOUT triggering an update (Watchtower's only endpoint performs the update,
// so the test hits the API root, not /v1/update).
function WatchtowerIntegration() {
  const [cfg, setCfg] = useState<UpdateConfig | null>(null);
  const [test, setTest] = useState<UpdateConnectionTest | null>(null);
  const [testing, setTesting] = useState(false);
  const [log, setLog] = useState<UpdateLog | null>(null);
  const [logLoading, setLogLoading] = useState(false);

  const loadLog = useCallback(async () => {
    setLogLoading(true);
    try {
      setLog(await api.updateLog(10));
    } catch {
      /* the card still works without the ledger */
    } finally {
      setLogLoading(false);
    }
  }, []);

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

  useEffect(() => {
    void loadLog();
  }, [loadLog]);

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
            Set <code>WATCHTOWER_URL</code> (and <code>WATCHTOWER_TOKEN</code>) in
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

        <Box sx={{ mt: 2 }}>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
            <Typography variant="subtitle2" sx={{ flexGrow: 1 }}>
              Update history
            </Typography>
            <Button size="small" onClick={() => void loadLog()} disabled={logLoading}>
              {logLoading ? "Loading…" : "Refresh"}
            </Button>
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
            Every &quot;Update now&quot; press, and whether the build actually changed.
            {log?.running_sha ? ` Running build: ${log.running_sha.slice(0, 7)}.` : ""}
          </Typography>
          {log && log.attempts.length > 0 ? (
            log.attempts.map((a) => <AttemptRow key={a.id} attempt={a} />)
          ) : (
            <Typography variant="caption" color="text.secondary">
              No update has been attempted yet.
            </Typography>
          )}
        </Box>
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
        {/* Two columns wide: the card carries the update ledger, whose per-attempt detail
            (why nothing changed, which build it went from and to) is unreadable in a third. */}
        <Box sx={{ gridColumn: { sm: "span 2" } }}>
          <WatchtowerIntegration />
        </Box>
      </Box>
    </Box>
  );
}

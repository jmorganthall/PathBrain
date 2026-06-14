import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import ExtensionIcon from "@mui/icons-material/Extension";
import ScienceIcon from "@mui/icons-material/Science";

import { api } from "../api/client";
import type { ExperimentsResponse, PluginInfo } from "../api/types";
import Loading from "../components/Loading";

export default function Plugins() {
  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [experiments, setExperiments] = useState<ExperimentsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [p, e] = await Promise.all([api.plugins(), api.experiments()]);
        setPlugins(p);
        setExperiments(e);
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

      <Card>
        <CardContent>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
            <ScienceIcon color="secondary" />
            <Typography variant="h6">Experiments Engine</Typography>
            {experiments && (
              <Chip
                size="small"
                label={experiments.status}
                color={experiments.status === "ok" ? "success" : "default"}
                variant="outlined"
              />
            )}
          </Stack>
          <Typography variant="body2" color="text.secondary">
            {experiments?.message ??
              "The experiments engine runs guided optimization trials against your network path."}
          </Typography>
          {experiments && experiments.experiments.length > 0 && (
            <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: "block" }}>
              {experiments.experiments.length} experiment(s) defined.
            </Typography>
          )}
        </CardContent>
      </Card>
    </Box>
  );
}

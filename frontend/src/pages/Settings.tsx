import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import { api } from "../api/client";
import type { SettingsImpact, SettingsProfile } from "../api/types";
import Loading from "../components/Loading";
import EmptyState from "../components/EmptyState";
import InsightsIcon from "@mui/icons-material/Insights";
import { fmtDateTime } from "../utils/format";

export function ImpactBanner({ impact }: { impact: SettingsImpact }) {
  if (!impact.changed || impact.delta_pct == null) return null;
  const improved = (impact.delta_abs ?? 0) >= 0;
  const arrow = improved ? "▲" : "▼";
  const severity = !impact.significant ? "info" : improved ? "success" : "warning";
  return (
    <Alert severity={severity} icon={<InsightsIcon />} sx={{ mb: 2 }}>
      <Typography variant="body2">
        Since the settings changed{impact.changed_at ? ` (${fmtDateTime(impact.changed_at)})` : ""},
        median SOPS went <b>{arrow} {Math.abs(impact.delta_pct)}%</b> (
        {impact.before?.median} → {impact.after?.median}).{" "}
        {impact.significant
          ? "This exceeds your significance threshold."
          : `Below the ${impact.threshold_pct}% significance threshold.`}
      </Typography>
      <Typography variant="caption" color="text.secondary">
        {impact.before?.label} → {impact.after?.label}
      </Typography>
    </Alert>
  );
}

export default function Settings() {
  const [profiles, setProfiles] = useState<SettingsProfile[] | null>(null);
  const [impact, setImpact] = useState<SettingsImpact | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [p, i] = await Promise.all([api.settingsProfiles(), api.settingsImpact()]);
        setProfiles(p.profiles);
        setImpact(i);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load settings analysis");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  if (loading) return <Loading label="Loading settings analysis…" />;

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 1 }}>
        Settings Impact
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        How your firewall/SQM configuration profiles correlate with the Seat of Pants Score. Each run
        is stamped with the settings that were live when it ran.
      </Typography>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}

      {impact && <ImpactBanner impact={impact} />}

      {!profiles || profiles.length === 0 ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={<InsightsIcon fontSize="inherit" />}
              title="No settings profiles yet"
              description="Once runs capture your firewall settings (requires the OPNsense provider with traffic-shaper access), each distinct configuration will appear here with its score distribution."
            />
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent>
            <Typography variant="h6" gutterBottom>
              Profiles ({profiles.length})
            </Typography>
            <Typography variant="caption" color="text.secondary">
              Ranked by median SOPS. Higher is better.
            </Typography>
            <TableContainer sx={{ mt: 1 }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Profile</TableCell>
                    <TableCell align="right">Runs</TableCell>
                    <TableCell align="right">Median</TableCell>
                    <TableCell align="right">IQR</TableCell>
                    <TableCell align="right">Min–Max</TableCell>
                    <TableCell>Last seen</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {profiles.map((p, idx) => (
                    <TableRow key={p.fingerprint}>
                      <TableCell sx={{ maxWidth: 360 }}>
                        <Stackish label={p.label} fingerprint={p.fingerprint} best={idx === 0} />
                      </TableCell>
                      <TableCell align="right">{p.count}</TableCell>
                      <TableCell align="right" sx={{ fontWeight: 700 }}>
                        {p.median}
                      </TableCell>
                      <TableCell align="right">
                        {p.p25}–{p.p75}
                      </TableCell>
                      <TableCell align="right">
                        {p.min}–{p.max}
                      </TableCell>
                      <TableCell>{fmtDateTime(p.last_seen)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          </CardContent>
        </Card>
      )}
    </Box>
  );
}

function Stackish({
  label,
  fingerprint,
  best,
}: {
  label: string;
  fingerprint: string;
  best: boolean;
}) {
  return (
    <Box>
      <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <Typography variant="body2" sx={{ wordBreak: "break-word" }}>
          {label}
        </Typography>
        {best && <Chip size="small" color="success" label="best" />}
      </Box>
      <Typography variant="caption" color="text.secondary">
        {fingerprint}
      </Typography>
    </Box>
  );
}

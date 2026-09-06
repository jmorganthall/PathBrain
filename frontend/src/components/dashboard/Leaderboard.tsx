// Top profiles by pooled Overall as thin horizontal bars — the "fastest profile by
// Overall" glance. One hue for every bar (colour follows nothing here: the bar length IS
// the ranking); the crown, the live profile and any statistical tie are marked with
// icons and chips, never by repainting a bar.
import { Link as RouterLink } from "react-router-dom";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import EmojiEventsIcon from "@mui/icons-material/EmojiEvents";
import SensorsIcon from "@mui/icons-material/Sensors";

import type { SettingsProfile } from "../../api/types";
import { fmtNum } from "../../utils/format";

interface Props {
  profiles: SettingsProfile[];
  bestFingerprint: string | null;
  currentFingerprint: string | null;
  coLeaders: string[];
  minIterations: number;
  limit?: number;
}

const BAR = "#4dd0e1";

export default function Leaderboard({
  profiles,
  bestFingerprint,
  currentFingerprint,
  coLeaders,
  minIterations,
  limit = 6,
}: Props) {
  const tied = new Set(coLeaders);
  // Confident profiles only, best Overall first — the same population the crown is
  // chosen from, so #1 here is the crown by construction.
  const ranked = profiles
    .filter((p) => p.confident && p.overall != null)
    .sort((a, b) => (b.overall ?? 0) - (a.overall ?? 0));
  const rows = ranked.slice(0, limit);
  // Keep the live profile in view even when it sits below the cut, so the reader can see
  // where "what the firewall is on" stands against the leaders.
  const liveIdx = ranked.findIndex((p) => p.fingerprint === currentFingerprint);
  const live = liveIdx >= limit ? ranked[liveIdx] : null;
  const shown = live ? [...rows, live] : rows;
  const rankOf = (fp: string) => ranked.findIndex((p) => p.fingerprint === fp) + 1;

  if (rows.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        No profile has reached confidence yet ({minIterations} iterations). Keep collecting —
        the leaderboard fills in as profiles mature.
      </Typography>
    );
  }

  // Bars share a 0–100 scale; the leaders sit in a narrow band at the top of it, so a
  // domain starting at the weakest shown score would exaggerate hair-width gaps. Keep
  // the true scale and let the numbers carry the precision.
  return (
    <Stack spacing={1}>
      {shown.map((p, i) => {
        const isCrown = p.fingerprint === bestFingerprint;
        const isLive = p.fingerprint === currentFingerprint;
        const rank = rankOf(p.fingerprint);
        const gapRow = live != null && i === shown.length - 1;
        return (
          <Box key={p.fingerprint} sx={{ borderTop: gapRow ? 1 : 0, borderColor: "divider", pt: gapRow ? 1 : 0 }}>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.4, minWidth: 0 }}>
              <Typography
                variant="caption"
                color="text.secondary"
                sx={{ width: 22, flexShrink: 0, fontVariantNumeric: "tabular-nums", textAlign: "right" }}
              >
                {rank}
              </Typography>
              {isCrown ? (
                <Tooltip title="The pooled crown — highest Overall among confident profiles.">
                  <EmojiEventsIcon sx={{ fontSize: 16, color: "warning.main", flexShrink: 0 }} />
                </Tooltip>
              ) : (
                <Box sx={{ width: 16, flexShrink: 0 }} />
              )}
              <Link
                component={RouterLink}
                to={`/profiles/${encodeURIComponent(p.fingerprint)}`}
                underline="hover"
                color="inherit"
                variant="body2"
                noWrap
                sx={{ flex: 1, minWidth: 0, fontWeight: isCrown ? 600 : 400 }}
                title={p.label}
              >
                {p.name || p.label}
              </Link>
              {isLive && (
                <Tooltip title="The profile the firewall is on right now.">
                  <Chip
                    size="small"
                    color="info"
                    variant="outlined"
                    icon={<SensorsIcon sx={{ fontSize: 14 }} />}
                    label="live"
                    sx={{ height: 20 }}
                  />
                </Tooltip>
              )}
              {tied.has(p.fingerprint) && (
                <Tooltip title="Statistically tied with the crown — its lead is inside run-to-run noise.">
                  <Chip size="small" variant="outlined" label="tied" sx={{ height: 20 }} />
                </Tooltip>
              )}
              <Typography
                variant="body2"
                sx={{ fontWeight: 600, width: 44, textAlign: "right", flexShrink: 0, fontVariantNumeric: "tabular-nums" }}
              >
                {fmtNum(p.overall, 1)}
              </Typography>
            </Stack>
            <Stack direction="row" spacing={1} alignItems="center">
              <Box sx={{ width: 22 + 8 + 16 + 8, flexShrink: 0 }} />
              <Box
                sx={{
                  flex: 1,
                  height: 6,
                  borderRadius: 3,
                  bgcolor: "rgba(255,255,255,0.06)",
                  overflow: "hidden",
                }}
              >
                <Box
                  sx={{
                    width: `${Math.max(0, Math.min(100, p.overall ?? 0))}%`,
                    height: "100%",
                    borderRadius: 3,
                    bgcolor: BAR,
                    opacity: isCrown ? 1 : 0.7,
                  }}
                />
              </Box>
              <Typography
                variant="caption"
                color="text.disabled"
                sx={{ width: 44, textAlign: "right", flexShrink: 0, fontVariantNumeric: "tabular-nums" }}
                title={`${p.iterations} iterations across ${p.count} runs`}
              >
                {p.iterations} it
              </Typography>
            </Stack>
          </Box>
        );
      })}
    </Stack>
  );
}

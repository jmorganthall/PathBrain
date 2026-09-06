// The duel ladder at a glance: the belt holder and the top of the standings. Ranked on
// the ring's own fitted strength (Elo-scale Bradley–Terry, ordered by its conservative
// floor), which is a different verdict from the pooled Overall the leaderboard shows —
// that is the point of showing both on one screen.
import { Link as RouterLink } from "react-router-dom";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import MilitaryTechIcon from "@mui/icons-material/MilitaryTech";

import type { DuelStandings } from "../../api/types";

interface Props {
  standings: DuelStandings;
  limit?: number;
}

export default function RingCard({ standings, limit = 5 }: Props) {
  const champ = standings.champion;
  const rows = standings.standings.slice(0, limit);
  if (rows.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        No matches on the ledger yet. Start a duel to adjudicate the crown head to head.
      </Typography>
    );
  }
  // Rating bars: a relative scale across the shown rows (Elo has no natural 0), anchored
  // so the weakest shown row still has a visible bar.
  const ratings = rows.map((r) => r.rating ?? 1500);
  const lo = Math.min(...ratings) - 40;
  const hi = Math.max(...ratings) + 10;
  const span = hi - lo || 1;

  return (
    <Stack spacing={1.25}>
      {champ && (
        <Stack direction="row" spacing={1} alignItems="center" sx={{ minWidth: 0 }}>
          <MilitaryTechIcon sx={{ color: "info.main" }} />
          <Box sx={{ minWidth: 0, flex: 1 }}>
            <Typography variant="body2" noWrap sx={{ fontWeight: 600 }} title={champ.label ?? undefined}>
              <Link
                component={RouterLink}
                to={`/profiles/${encodeURIComponent(champ.fingerprint)}`}
                underline="hover"
                color="inherit"
              >
                {champ.name || champ.label || champ.fingerprint}
              </Link>
            </Typography>
            <Typography variant="caption" color="text.secondary" noWrap component="div">
              holds the belt
              {champ.defences != null ? ` · ${champ.defences} defence${champ.defences === 1 ? "" : "s"}` : ""}
              {champ.rank != null ? ` · #${champ.rank} on rating` : ""}
              {champ.provisional ? " · provisional" : ""}
            </Typography>
          </Box>
        </Stack>
      )}
      {rows.map((r) => {
        const rating = r.rating ?? 1500;
        const pct = ((rating - lo) / span) * 100;
        const isChamp = champ?.fingerprint === r.fingerprint;
        return (
          <Box key={r.fingerprint}>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.4, minWidth: 0 }}>
              <Typography
                variant="caption"
                color="text.secondary"
                sx={{ width: 22, flexShrink: 0, textAlign: "right", fontVariantNumeric: "tabular-nums" }}
              >
                {r.rank}
              </Typography>
              <Link
                component={RouterLink}
                to={`/profiles/${encodeURIComponent(r.fingerprint)}`}
                underline="hover"
                color="inherit"
                variant="body2"
                noWrap
                sx={{ flex: 1, minWidth: 0, fontWeight: isChamp ? 600 : 400 }}
                title={r.label}
              >
                {r.name || r.label}
              </Link>
              {r.tied_with_leader && (
                <Tooltip title="Not clearly below the leader — the gap is inside the ring's noise.">
                  <Chip size="small" variant="outlined" label="tied" sx={{ height: 20 }} />
                </Tooltip>
              )}
              <Typography
                variant="caption"
                color="text.secondary"
                sx={{ flexShrink: 0, fontVariantNumeric: "tabular-nums" }}
                title="wins–losses–draws"
              >
                {r.wins}–{r.losses}–{r.draws}
              </Typography>
              <Typography
                variant="body2"
                sx={{ fontWeight: 600, width: 48, textAlign: "right", flexShrink: 0, fontVariantNumeric: "tabular-nums" }}
                title={r.rating_se != null ? `rating ± ${Math.round(r.rating_se)}` : undefined}
              >
                {Math.round(rating)}
                {r.rating_provisional ? "?" : ""}
              </Typography>
            </Stack>
            <Stack direction="row" spacing={1} alignItems="center">
              <Box sx={{ width: 30, flexShrink: 0 }} />
              <Box sx={{ flex: 1, height: 6, borderRadius: 3, bgcolor: "rgba(255,255,255,0.06)", overflow: "hidden" }}>
                <Box
                  sx={{
                    width: `${Math.max(4, Math.min(100, pct))}%`,
                    height: "100%",
                    borderRadius: 3,
                    bgcolor: "#7c4dff",
                    opacity: isChamp ? 1 : 0.7,
                  }}
                />
              </Box>
              <Box sx={{ width: 48, flexShrink: 0 }} />
            </Stack>
          </Box>
        );
      })}
      <Typography variant="caption" color="text.disabled">
        {standings.decisive_matchups} decided of {standings.matchups_analyzed} match
        {standings.matchups_analyzed === 1 ? "" : "es"} over {standings.sessions_analyzed} session
        {standings.sessions_analyzed === 1 ? "" : "s"} · rating on the Elo scale, ? = provisional
      </Typography>
    </Stack>
  );
}

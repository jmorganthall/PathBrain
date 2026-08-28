// The two crowns — PathBrain's two ways of naming a best profile, shown side by side.
//
// They answer different questions and are allowed to disagree:
//
//   • **Pooled crown** (trophy) — the observational argmax over ALL history. Every run
//     a profile ever recorded, in every condition, averaged into one standing. Broad,
//     but profiles are compared across different weather at different times.
//   • **Duel champion** (belt) — the controlled trial. Interleaved A/B/A/B bouts where
//     both sides met the same weather by construction, decided by a sequential test.
//     Narrow (only the contenders that stepped in the ring), but confound-free.
//
// When they agree, that's the strongest signal available. When they disagree, that's a
// prompt to look — not an error. Only the one marked "following" is what automation
// acts on, which is the crowning policy's job (top-bar Follow best).
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import EmojiEventsIcon from "@mui/icons-material/EmojiEvents";
import MilitaryTechIcon from "@mui/icons-material/MilitaryTech";
import HandshakeIcon from "@mui/icons-material/Handshake";

import { api } from "../api/client";
import type { CrownsOut } from "../api/types";
import { fmtDateTime, fmtNum } from "../utils/format";

const fmtReign = (hours: number | null | undefined) => {
  if (hours == null) return "—";
  if (hours < 1) return "under an hour";
  if (hours < 48) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
};

// One crown tile. `tone` colors the icon so the two are distinguishable at a glance even
// before you read the label: amber trophy for pooled, blue-grey belt for the duel.
function CrownTile({
  icon,
  kind,
  what,
  title,
  subtitle,
  detail,
  governing,
  muted,
  onOpen,
  href,
}: {
  icon: React.ReactNode;
  kind: string;
  what: string;
  title: string;
  subtitle: string;
  detail?: string;
  governing: boolean;
  muted?: boolean;
  onOpen?: () => void;
  href: string;
}) {
  const navigate = useNavigate();
  return (
    <Box
      sx={{
        flex: 1,
        minWidth: 0,
        p: 1.5,
        borderRadius: 2,
        border: 1,
        borderColor: governing ? "primary.main" : "divider",
        bgcolor: governing ? "action.selected" : "transparent",
        opacity: muted ? 0.7 : 1,
      }}
    >
      <Stack direction="row" spacing={1.5} alignItems="flex-start">
        <Tooltip title={what}>
          <Box sx={{ display: "flex", pt: 0.25 }}>{icon}</Box>
        </Tooltip>
        <Box sx={{ minWidth: 0, flex: 1 }}>
          <Stack direction="row" spacing={0.75} alignItems="center" flexWrap="wrap" useFlexGap>
            <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1.4 }}>
              {kind}
            </Typography>
            {governing ? (
              <Tooltip title="Automation acts on this verdict — the crowning policy in the top-bar Follow best popover decides which.">
                <Chip size="small" color="primary" label="following" sx={{ height: 18 }} />
              </Tooltip>
            ) : (
              <Tooltip title="Recorded and displayed, but automation isn't acting on it. Switch the crowning policy in the top-bar Follow best popover to change that.">
                <Chip size="small" variant="outlined" label="for reference" sx={{ height: 18 }} />
              </Tooltip>
            )}
          </Stack>
          <Typography variant="subtitle1" noWrap sx={{ lineHeight: 1.3 }}>
            <Link
              component="button"
              underline="hover"
              onClick={() => (onOpen ? onOpen() : navigate(href))}
              sx={{ font: "inherit", textAlign: "left" }}
            >
              {title}
            </Link>
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
            {subtitle}
          </Typography>
          {!!detail && (
            <Typography
              variant="caption"
              color="text.disabled"
              // Clamped to two lines: the detail is a full settings summary, which on a phone
              // pushes the crown that matters — the name and its Overall — off the card in
              // favour of "q4814 t3ms i60ms ecn". The whole string stays as the hover.
              title={detail}
              sx={{
                display: "-webkit-box",
                WebkitLineClamp: 2,
                WebkitBoxOrient: "vertical",
                overflow: "hidden",
              }}
            >
              {detail}
            </Typography>
          )}
        </Box>
      </Stack>
    </Box>
  );
}

export default function TwoCrowns() {
  const navigate = useNavigate();
  const [data, setData] = useState<CrownsOut | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.crowns());
    } catch {
      /* transient — the card just stays hidden */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Nothing crowned either way yet (a fresh install): don't show an empty scaffold.
  if (!data || (!data.pooled && !data.duel)) return null;

  const { pooled, duel, governing, agree } = data;
  const delta = data.overall_delta ?? null;

  return (
    <Card sx={{ mb: 2 }}>
      <CardContent>
        <Stack
          direction="row"
          spacing={1}
          alignItems="center"
          justifyContent="space-between"
          sx={{ mb: 1 }}
        >
          <Box>
            <Typography variant="h6">The two crowns</Typography>
            <Typography variant="caption" color="text.secondary">
              Two ways of naming the best profile — the all-history standings and the
              head-to-head ring. Only the "following" one is applied.
            </Typography>
          </Box>
          {agree && (
            <Tooltip title="The all-history standings and the head-to-head trial name the same profile — the strongest signal you can get from this data.">
              <Chip
                size="small"
                color="success"
                icon={<HandshakeIcon />}
                label="both agree"
                sx={{ flexShrink: 0 }}
              />
            </Tooltip>
          )}
        </Stack>

        <Stack direction={{ xs: "column", md: "row" }} spacing={1.5}>
          {pooled ? (
            <CrownTile
              icon={<EmojiEventsIcon sx={{ color: "warning.main", fontSize: 34 }} />}
              kind="Overall crown"
              what="Best profile across ALL measured history — every run in every condition, pooled into one standing. Broad, but it compares profiles measured at different times."
              title={pooled.name || pooled.label || pooled.fingerprint}
              subtitle={`Overall ${fmtNum(pooled.overall_now ?? pooled.overall, 1)} · holding for ${fmtReign(
                pooled.reign_hours
              )}`}
              detail={[pooled.name ? pooled.label : null, pooled.since ? `crowned ${fmtDateTime(pooled.since)}` : null]
                .filter(Boolean)
                .join(" · ")}
              governing={governing.source === "pooled"}
              href={`/profiles/${encodeURIComponent(pooled.fingerprint)}`}
            />
          ) : (
            <CrownTile
              icon={<EmojiEventsIcon sx={{ color: "text.disabled", fontSize: 34 }} />}
              kind="Overall crown"
              what="Best profile across all measured history."
              title="Not crowned yet"
              subtitle="No profile has reached confidence — keep collecting runs."
              governing={governing.source === "pooled"}
              muted
              href="/settings"
              onOpen={() => navigate("/settings")}
            />
          )}

          {duel ? (
            <CrownTile
              icon={<MilitaryTechIcon sx={{ color: "info.main", fontSize: 34 }} />}
              kind="Duel champion"
              what="Winner of the head-to-head ladder: interleaved A/B/A/B bouts where both sides met the same weather, decided by a sequential test. Narrower than the overall crown, but free of the timing confound."
              title={duel.name || duel.label || duel.fingerprint}
              subtitle={`${
                duel.overall_now != null ? `Overall ${fmtNum(duel.overall_now, 1)} · ` : ""
              }${duel.wins}–${duel.losses}–${duel.draws} in the ring · ${
                duel.consecutive_sessions
              } session${duel.consecutive_sessions === 1 ? "" : "s"} as champion`}
              detail={
                duel.fresh
                  ? `Won duel #${duel.duel_id} · ${fmtDateTime(duel.finished_at)}${
                      duel.decisive ? "" : " · by draws only"
                    }`
                  : `Verdict expired (older than ${duel.freshness_days}d) — run another duel to renew it`
              }
              governing={governing.source === "duel"}
              muted={!duel.fresh}
              href="/duels"
              onOpen={() => navigate("/duels")}
            />
          ) : (
            <CrownTile
              icon={<MilitaryTechIcon sx={{ color: "text.disabled", fontSize: 34 }} />}
              kind="Duel champion"
              what="Winner of the head-to-head ladder — decided by interleaved same-weather bouts."
              title="No duel yet"
              subtitle="Run a duel to adjudicate the crown head to head."
              governing={governing.source === "duel"}
              muted
              href="/duels"
              onOpen={() => navigate("/duels")}
            />
          )}
        </Stack>

        {!agree && pooled && duel && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.5 }}>
            The two disagree — normal, and worth a look: the overall crown wins on the whole
            record, the champion won its bouts under matched conditions.
            {delta != null && (
              <>
                {" "}On the pooled record the champion measures{" "}
                <b>{delta > 0 ? `${fmtNum(delta, 1)} ahead of` : delta < 0 ? `${fmtNum(Math.abs(delta), 1)} behind` : "level with"}</b>{" "}
                the overall crown{delta < 0 ? " — its head-to-head wins came under matched conditions the pooled average doesn't see" : delta > 0 ? " as well — the pooled record may simply be lagging the ring" : ""}.
              </>
            )}{" "}
            More duels (or more runs) will resolve it.
          </Typography>
        )}
      </CardContent>
    </Card>
  );
}

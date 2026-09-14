// **Which profile should I run?** — the one answer, at the top of the page.
//
// PathBrain measures a great deal and, until this card, said so in pieces: the two crowns
// side by side with "following" / "for reference" chips, the ring's #1 as a third claim on
// the Duels page, and a standings table of rating, proven floor, points, win rate, pair
// rate and median margin. Every one of those is true and none of them is the sentence a
// person opens the dashboard for — reported, exactly: *"we've got ALL of these metrics and
// numbers and crowns and win rate and pts and margin — where is the THIS IS THE BEST
// PROFILE rating?"*
//
// Three deliberate choices about what this says and what it refuses to say:
//
//   • **It names a profile, always.** The answer is the argmax among confident profiles —
//     no floor, no hysteresis — because a profile better by a hair is better. A tie is an
//     annotation on that answer, never a replacement for it: "Palm Oyster, and fourteen
//     profiles are tied with it" is complete, while "it's a tie" has declined to answer.
//   • **It prices the choice.** The crown's lead over the unshaped baseline is on the card
//     beside the lead over the runner-up, because a reader deciding where to spend a night
//     needs to know shaping is worth a couple of points and the pick between the tied
//     leaders is worth a fraction of one. Without that, the ranking looks like the
//     important question when it is the small one.
//   • **It never offers a firewall change.** It says what to run and whether you are
//     running it; applying is the crowning policy's decision and lives behind the profile.
import { useCallback, useEffect, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import EmojiEventsIcon from "@mui/icons-material/EmojiEvents";

import { api } from "../../api/client";
import type { VerdictOut, VerdictProfile } from "../../api/types";
import { fmtNum } from "../../utils/format";
import { sopsColor } from "../../theme";

const nameOf = (p: VerdictProfile) => p.name || p.label || p.fingerprint.slice(0, 8);

/** One "tied with the leader" pill, linking to the profile it names. */
function TiedChip({ p }: { p: VerdictProfile }) {
  return (
    <Tooltip title={`Overall ${fmtNum(p.overall, 1)} over ${p.iterations} iterations`}>
      <Chip
        size="small"
        variant="outlined"
        clickable
        component={RouterLink}
        to={`/profiles/${encodeURIComponent(p.fingerprint)}`}
        label={nameOf(p)}
      />
    </Tooltip>
  );
}

export default function VerdictCard() {
  const [data, setData] = useState<VerdictOut | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.verdict());
    } catch {
      /* transient — the card stays hidden rather than showing a broken answer */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (!data) return null;

  const { best, tied, tied_count: tiedCount, lead, noise_bar: bar, clear } = data;

  // No answer — either nothing is confident yet, or the methodology's crown is
  // field-relative and this card declines rather than contradicting the standings. Still
  // worth a card either way: "measure one and this becomes an answer" is an instruction,
  // where an absent card is just an absence.
  if (!best) {
    return (
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack direction="row" spacing={1.5} alignItems="center">
            <EmojiEventsIcon sx={{ color: "text.disabled", fontSize: 34 }} />
            <Box>
              <Typography variant="overline" color="text.secondary">
                Which profile to run
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {data.verdict}
              </Typography>
            </Box>
          </Stack>
        </CardContent>
      </Card>
    );
  }

  const onIt = data.on_firewall === true;

  return (
    <Card sx={{ mb: 2, borderLeft: 4, borderColor: "primary.main" }}>
      <CardContent>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={2} alignItems={{ sm: "center" }}>
          <Stack direction="row" spacing={1.5} alignItems="center" sx={{ minWidth: 0, flex: 1 }}>
            <EmojiEventsIcon sx={{ color: "warning.main", fontSize: 40, flexShrink: 0 }} />
            <Box sx={{ minWidth: 0 }}>
              <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1.4 }}>
                Run this profile
              </Typography>
              <Typography variant="h5" sx={{ lineHeight: 1.2 }}>
                <Box
                  component={RouterLink}
                  to={`/profiles/${encodeURIComponent(best.fingerprint)}`}
                  sx={{ color: "inherit", textDecoration: "none", "&:hover": { textDecoration: "underline" } }}
                >
                  {nameOf(best)}
                </Box>
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                Best of {data.confident_profiles} profile{data.confident_profiles === 1 ? "" : "s"} measured
                to {data.min_iterations}+ iterations · {best.iterations} iterations here
              </Typography>
            </Box>
          </Stack>

          {/* The number the answer rests on, and whether the firewall is actually on it —
              "what should I run" is much less useful without "and are you running it". */}
          <Stack direction="row" spacing={2} alignItems="center" sx={{ flexShrink: 0 }}>
            <Box sx={{ textAlign: "center" }}>
              <Typography variant="h4" sx={{ fontWeight: 700, color: sopsColor(best.overall), lineHeight: 1 }}>
                {fmtNum(best.overall, 1)}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                Overall
              </Typography>
            </Box>
            {data.on_firewall != null && (
              <Tooltip
                title={
                  onIt
                    ? "The firewall is on this profile now."
                    : "The firewall is on a different profile. Applying is the crowning policy's job — arm Follow best, or apply it from the profile page."
                }
              >
                <Chip
                  size="small"
                  color={onIt ? "success" : "default"}
                  variant={onIt ? "filled" : "outlined"}
                  icon={onIt ? <CheckCircleIcon /> : undefined}
                  label={onIt ? "running now" : `running ${data.live ? nameOf(data.live) : "something else"}`}
                  sx={{ maxWidth: 220 }}
                />
              </Tooltip>
            )}
          </Stack>
        </Stack>

        <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>
          {data.verdict}
        </Typography>

        {/* The two facts that make the verdict usable, as chips rather than prose repeats. */}
        <Stack direction="row" spacing={1} sx={{ mt: 1.5 }} flexWrap="wrap" useFlexGap alignItems="center">
          {lead != null && bar != null && (
            <Tooltip
              title={
                clear
                  ? "The lead is larger than the run-to-run noise in both medians — a real ordering."
                  : "The lead is inside the run-to-run noise in the two medians, so the ordering is not demonstrated. The higher median still wins; it just hasn't been shown to."
              }
            >
              <Chip
                size="small"
                color={clear ? "success" : "default"}
                variant={clear ? "filled" : "outlined"}
                label={`+${fmtNum(lead, 2)} over next · noise ±${fmtNum(bar, 2)}`}
              />
            </Tooltip>
          )}
          {data.vs_sqm_off != null && (
            <Tooltip title="How much better the crown measures than the best unshaped (SQM off) baseline — what shaping itself is worth, next to what picking a profile is worth.">
              <Chip
                size="small"
                variant="outlined"
                color={data.vs_sqm_off > 0 ? "success" : "warning"}
                label={`${data.vs_sqm_off > 0 ? "+" : ""}${fmtNum(data.vs_sqm_off, 1)}% vs no shaper`}
              />
            </Tooltip>
          )}
          {data.resolves != null && (
            <Tooltip title="The smallest Overall gap the duel ladder can settle at its current round noise and pair cap. A real difference below this cannot reach a verdict there, however long it runs.">
              <Chip
                size="small"
                variant="outlined"
                component={RouterLink}
                to="/duels"
                clickable
                label={`ring resolves ${fmtNum(data.resolves, 2)}`}
              />
            </Tooltip>
          )}
        </Stack>

        {tiedCount > 0 && (
          <Box sx={{ mt: 1.5, pt: 1.5, borderTop: 1, borderColor: "divider" }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.75 }}>
              Statistically tied with it ({tiedCount}) — the evidence does not separate these,
              so any of them is a defensible thing to run:
            </Typography>
            <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap>
              {tied.map((p) => (
                <TiedChip key={p.fingerprint} p={p} />
              ))}
              {tiedCount > tied.length && (
                <Chip
                  size="small"
                  variant="outlined"
                  component={RouterLink}
                  to="/settings"
                  clickable
                  label={`+${tiedCount - tied.length} more`}
                />
              )}
            </Stack>
          </Box>
        )}
      </CardContent>
    </Card>
  );
}

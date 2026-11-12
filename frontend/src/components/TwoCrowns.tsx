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
import { Link as RouterLink, useNavigate } from "react-router-dom";
import Alert from "@mui/material/Alert";
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
import SpeedIcon from "@mui/icons-material/Speed";
import ImageIcon from "@mui/icons-material/Image";

import { api } from "../api/client";
import type { CrownsOut, PaperCost, FastestLegs, LegGaps} from "../api/types";
import { fmtDateTime, fmtNum } from "../utils/format";
import PaperCostLine from "./PaperCostLine";
import FastestGapLine from "./FastestGapLine";

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
  cost, gaps, fastest}: {
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
  cost?: PaperCost | null;
  gaps?: LegGaps | null;
  fastest?: FastestLegs | null;
}) {
  const navigate = useNavigate();
  return (
    <Box
      sx={{
        flex: 1,
        minWidth: 0,
        p: 2,
        borderRadius: 2,
        border: 1,
        borderColor: governing ? "primary.main" : "divider",
        bgcolor: governing ? "action.selected" : "transparent",
        opacity: muted ? 0.7 : 1,
      }}
    >
      <Stack direction="row" spacing={2} alignItems="flex-start">
        <Tooltip title={what}>
          <Box sx={{ display: "flex", pt: 0.5 }}>{icon}</Box>
        </Tooltip>
        <Box sx={{ minWidth: 0, flex: 1 }}>
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
            <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1.4 }}>
              {kind}
            </Typography>
            {governing ? (
              <Tooltip title="Auto-apply acts on this verdict. Which ranking it follows is chosen in the top-bar Auto-apply popover.">
                <Chip size="small" color="primary" label="following" sx={{ height: 18 }} />
              </Tooltip>
            ) : (
              <Tooltip title="Recorded and displayed, but Auto-apply isn't acting on it. Change which ranking it follows in the top-bar Auto-apply popover.">
                <Chip size="small" variant="outlined" label="for reference" sx={{ height: 18 }} />
              </Tooltip>
            )}
          </Stack>
          <Typography variant="subtitle1" noWrap sx={{ lineHeight: 1.3 }}>
            <Link
              component="button"
             
              onClick={() => (onOpen ? onOpen() : navigate(href))}
              sx={{ font: "inherit", textAlign: "left" }}
            >
              {title}
            </Link>
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
            {subtitle}
          </Typography>
          <FastestGapLine gaps={gaps} fastest={fastest} />
          <PaperCostLine cost={cost} />
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
            <Typography variant="h6">Two ways of naming a winner</Typography>
            <Typography variant="caption" color="text.secondary">
              The all-history average and the head-to-head duels can disagree.{" "}
              {governing.source === "fused"
                ? "Auto-apply follows neither alone: it acts on the combined ranking that fits both together."
                : 'Only the one marked "following" is applied.'}
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

        {/* The speed budget passed the policy's choice over: say which crown, by how much,
            and what Auto-apply follows instead — the trade made visible, not implied. */}
        {governing.speed_budget?.replaced && (
          <Alert severity="warning" variant="outlined" sx={{ mb: 2, py: 0 }}>
            Over the speed budget: {governing.detail}.
          </Alert>
        )}
        {governing.speed_budget?.within === false && !governing.speed_budget?.replaced && (
          <Alert severity="warning" variant="outlined" sx={{ mb: 2, py: 0 }}>
            {governing.detail}.
          </Alert>
        )}

        <Stack direction={{ xs: "column", md: "row" }} spacing={2}>
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
              cost={pooled.vs_paper}
              gaps={pooled.vs_fastest}
              fastest={data.fastest}
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
              what={
                duel.rule === "rating"
                  ? "The ring's #1 by fitted head-to-head rating over the whole ladder: interleaved bouts where both sides met the same weather. Narrower than the overall crown, but free of the timing confound. (The lineal belt — who defends — is on the Duels page.)"
                  : "Winner of the head-to-head ladder: interleaved A/B/A/B bouts where both sides met the same weather, decided by a sequential test. Narrower than the overall crown, but free of the timing confound."
              }
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
              cost={duel.vs_paper}
              gaps={duel.vs_fastest}
              fastest={data.fastest}
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

          {data.math && (
            <CrownTile
              icon={<SpeedIcon sx={{ color: "success.main", fontSize: 34 }} />}
              kind="Fastest on paper"
              what="The mathematical crown: the confident profile with the lowest median FCP × median LCP in raw milliseconds. No perception curve and no weights, so it is not a judgement of how a load feels. Auto-apply follows it only if you choose it."
              title={data.math.name || data.math.label || data.math.fingerprint}
              subtitle={`FCP ${fmtNum(data.math.fcp_ms, 0)} ms × LCP ${fmtNum(data.math.lcp_ms, 0)} ms`}
              detail={
                data.math.runner_up
                  ? `next: ${data.math.runner_up.name || data.math.runner_up.label} · ${fmtNum(
                      data.math.runner_up.fcp_ms,
                      0
                    )} × ${fmtNum(data.math.runner_up.lcp_ms, 0)} ms`
                  : `${data.math.iterations} iterations`
              }
              governing={governing.source === "math"}
              gaps={data.math.vs_fastest}
              fastest={data.fastest}
              href={`/profiles/${encodeURIComponent(data.math.fingerprint)}`}
            />
          )}
          {data.lcp_first && (
            <CrownTile
              icon={<ImageIcon sx={{ color: "secondary.main", fontSize: 34 }} />}
              kind="LCP first · candidate"
              what={`Main content first, then first paint: the fastest median FCP among confident profiles whose median LCP is within ${fmtNum(data.lcp_first.band_pct, 0)}% of the best. Raw milliseconds, no weights.`}
              title={data.lcp_first.name || data.lcp_first.label || data.lcp_first.fingerprint}
              subtitle={`LCP ${fmtNum(data.lcp_first.lcp_ms, 0)} ms · FCP ${fmtNum(data.lcp_first.fcp_ms, 0)} ms`}
              detail={
                data.lcp_first.lcp_behind_pct > 0
                  ? `${fmtNum(data.lcp_first.lcp_behind_pct, 1)}% behind the best LCP for ${fmtNum(
                      data.lcp_first.fcp_gain_ms,
                      0
                    )} ms faster FCP · ${data.lcp_first.qualifiers} qualified`
                  : `best LCP and fastest FCP of ${data.lcp_first.qualifiers} qualifying`
              }
              governing={governing.source === "lcp_first"}
              gaps={data.lcp_first.vs_fastest}
              fastest={data.fastest}
              href={`/profiles/${encodeURIComponent(data.lcp_first.fingerprint)}`}
            />
          )}
          {data.fcp_first && (
            <CrownTile
              icon={<ImageIcon sx={{ color: "info.main", fontSize: 34 }} />}
              kind="FCP first · candidate"
              what={`First paint first, then main content: the fastest median LCP among confident profiles whose median FCP is within ${fmtNum(data.fcp_first.band_pct, 0)}% of the best. Raw milliseconds, no weights.`}
              title={data.fcp_first.name || data.fcp_first.label || data.fcp_first.fingerprint}
              subtitle={`FCP ${fmtNum(data.fcp_first.fcp_ms, 0)} ms · LCP ${fmtNum(data.fcp_first.lcp_ms, 0)} ms`}
              detail={
                data.fcp_first.fcp_behind_pct > 0
                  ? `${fmtNum(data.fcp_first.fcp_behind_pct, 1)}% behind the best FCP for ${fmtNum(
                      data.fcp_first.lcp_gain_ms,
                      0
                    )} ms faster LCP · ${data.fcp_first.qualifiers} qualified`
                  : `best FCP and fastest LCP of ${data.fcp_first.qualifiers} qualifying`
              }
              governing={governing.source === "fcp_first"}
              gaps={data.fcp_first.vs_fastest}
              fastest={data.fastest}
              href={`/profiles/${encodeURIComponent(data.fcp_first.fingerprint)}`}
            />
          )}
        </Stack>

        {governing.source === "fused" && (
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 2 }} flexWrap="wrap" useFlexGap>
            <Chip size="small" color="primary" label="following · combined ranking" sx={{ height: 20 }} />
            <Typography variant="caption" color="text.secondary">
              {data.fused ? (
                <>
                  The fit names <b>{data.fused.name || data.fused.label || data.fused.fingerprint.slice(0, 8)}</b>
                  {" "}(Overall {fmtNum(data.fused.fused, 1)} ± {fmtNum(data.fused.fused_se, 2)}
                  {data.fused.tied_count ? `, ${data.fused.tied_count} tied` : ""}).{" "}
                  <FastestGapLine gaps={data.fused.vs_fastest} fastest={data.fastest} />
                  <PaperCostLine cost={data.fused.vs_paper} />
                </>
              ) : (
                <>The fit names nobody yet, so the pooled crown stands in. </>
              )}
              <Link component={RouterLink} to="/overall">
                See how on the Best profile page
              </Link>
              .
            </Typography>
          </Stack>
        )}

        {!agree && pooled && duel && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 2 }}>
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

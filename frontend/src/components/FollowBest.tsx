// Top-bar "Follow best" control: a switch that arms the crown follower (keep the
// firewall's SQM settings on the crowned best profile as the crown changes), plus a
// popover with the follower's status, the crown-churn statistics ("how often does the
// best profile change?"), and the recent crown-change ledger. Tracking (the ledger)
// runs whether or not the switch is on — the switch only arms the firewall write.
import { useCallback, useEffect, useState } from "react";
import FastestGapLine from "./FastestGapLine";
import { useFastestGaps } from "../hooks/useFastestGaps";
import { Link as RouterLink } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Collapse from "@mui/material/Collapse";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import Link from "@mui/material/Link";
import Popover from "@mui/material/Popover";
import Radio from "@mui/material/Radio";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import EmojiEventsIcon from "@mui/icons-material/EmojiEvents";

import { api } from "../api/client";
import type { CrownFollowStatus, CrowningPolicy, CrownJudgement } from "../api/types";
import { fmtTimeShort } from "../utils/format";

const POLL_MS = 60_000;

function fmtHours(h: number | null | undefined): string {
  if (h == null || Number.isNaN(h)) return "—";
  if (h < 1) return `${Math.round(h * 60)} min`;
  if (h < 48) return `${h.toFixed(h < 10 ? 1 : 0)} h`;
  return `${(h / 24).toFixed(1)} d`;
}

export default function FollowBest() {
  const [info, setInfo] = useState<CrownFollowStatus | null>(null);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [busy, setBusy] = useState(false); // toggle or sync in flight
  const [snack, setSnack] = useState<{ msg: string; sev: "success" | "error" | "info" } | null>(
    null,
  );

  const load = useCallback(() => {
    api
      .crownFollow()
      .then(setInfo)
      .catch(() => {});
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  const enabled = info?.config.enabled ?? false;
  const policy = info?.config.policy ?? "fused";

  // ── The draft: pick a ranking, set its options, then Apply ─────────────────────────
  // Nothing below the ranking list saves on its own. The choice, the chosen ranking's
  // band and the speed budget are edited as one draft and written together by Apply, so
  // the follower acts on a settled choice instead of on every click and keystroke on the
  // way to it. The on/off switch stays instant: it is the master switch, not a setting.
  const saved = draftFrom(info);
  const [draft, setDraft] = useState<Draft>(saved);
  const dirty = !sameDraft(draft, saved);
  const savedKey = JSON.stringify(saved);
  useEffect(() => {
    // Follow the server while nothing is being edited (the 60 s poll, another tab).
    if (!dirty) setDraft(saved);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [savedKey]);
  const openPopover = (el: HTMLElement) => {
    setDraft(saved); // every visit starts from what is actually saved
    setAnchor(el);
  };
  const edit = (patch: Partial<Draft>) => setDraft((d) => ({ ...d, ...patch }));

  const apply = () => {
    const problem = validateDraft(draft);
    if (problem) {
      setSnack({ msg: problem, sev: "error" });
      return;
    }
    const body: Parameters<typeof api.crownFollowUpdate>[0] = {};
    if (draft.policy !== saved.policy) body.policy = draft.policy;
    if (Number(draft.lcpBand) !== Number(saved.lcpBand)) body.lcp_band_pct = Number(draft.lcpBand);
    if (Number(draft.fcpBand) !== Number(saved.fcpBand)) body.fcp_band_pct = Number(draft.fcpBand);
    const sb: { enabled?: boolean; lcp_pct?: number; fcp_pct?: number } = {};
    if (draft.budgetOn !== saved.budgetOn) sb.enabled = draft.budgetOn;
    if (Number(draft.budgetLcp) !== Number(saved.budgetLcp)) sb.lcp_pct = Number(draft.budgetLcp);
    if (Number(draft.budgetFcp) !== Number(saved.budgetFcp)) sb.fcp_pct = Number(draft.budgetFcp);
    if (Object.keys(sb).length) body.speed_budget = sb;
    if (!Object.keys(body).length) return;
    setBusy(true);
    api
      .crownFollowUpdate(body)
      .then(() => {
        const name = RANKINGS.find((r) => r.policy === draft.policy)?.label ?? draft.policy;
        setSnack({
          msg: enabled
            ? `Applied: following ${name}. The firewall switches within a minute if that names a different profile.`
            : `Saved: ${name} is the ranking to follow. Auto-apply is off, so the firewall stays as it is.`,
          sev: "success",
        });
        load();
      })
      .catch((e) => setSnack({ msg: e instanceof Error ? e.message : "Could not save.", sev: "error" }))
      .finally(() => setBusy(false));
  };

  const toggle = (next: boolean) => {
    setBusy(true);
    // Optimistic flip so the switch feels instant; reconcile with the server response.
    setInfo((v) => (v ? { ...v, config: { ...v.config, enabled: next } } : v));
    api
      .crownFollowUpdate({ enabled: next })
      .then(() => {
        setSnack(
          next
            ? {
                msg: "Auto-apply is on — the firewall will be kept on the best profile.",
                sev: "success",
              }
            : { msg: "Auto-apply is off — changes of the best profile are still recorded.", sev: "info" },
        );
        load();
      })
      .catch((e) => {
        setSnack({ msg: e instanceof Error ? e.message : "Could not update.", sev: "error" });
        load();
      })
      .finally(() => setBusy(false));
  };

  const syncNow = () => {
    setBusy(true);
    api
      .crownFollowSync()
      .then(({ result }) => {
        if (result.applied) setSnack({ msg: "Crown applied to the firewall.", sev: "success" });
        else if (result.error) setSnack({ msg: result.error, sev: "error" });
        else if (result.apply_skipped)
          setSnack({ msg: result.apply_skipped, sev: "info" });
        else if (result.on_crown)
          setSnack({ msg: "Firewall is already on the crowned profile.", sev: "success" });
        else if (!result.crown_fingerprint)
          setSnack({ msg: "No confident crown yet — nothing to follow.", sev: "info" });
        else setSnack({ msg: "Checked.", sev: "info" });
        load();
      })
      .catch((e) => {
        setSnack({ msg: e instanceof Error ? e.message : "Check failed.", sev: "error" });
      })
      .finally(() => setBusy(false));
  };

  const stats = info?.stats;
  const last = info?.status.last_result;
  // The profile the follower actually writes, per the crowning policy. Falls back to the
  // pooled crown for a check that predates the policy fields.
  const followedFp = last?.governing_fingerprint ?? last?.crown_fingerprint ?? null;
  const followedName =
    last?.governing_name || last?.governing_label || last?.crown_name || followedFp;
  const followedLabel = last?.governing_label ?? last?.crown_label ?? null;
  const followedSource = last?.governing_source ?? "fused";
  // What the followed profile (and the duel champion) give up against the field's fastest
  // FCP and LCP — the trade automation is making, stated where it is chosen.
  const legGaps = useFastestGaps([followedFp, info?.duel_champion?.fingerprint]);
  const pooledDiffers = !!(
    last?.crown_fingerprint &&
    followedFp &&
    last.crown_fingerprint !== followedFp
  );
  const changes = (info?.events ?? []).filter((e) => e.kind === "change").slice(0, 5);
  // Under a methodology that states its judgement, the version decides the crown and
  // the policy and budget controls would move nothing, so the card says who decides instead.
  const judged = info?.config.judgement ?? null;

  return (
    <>
      <Box sx={{ display: "flex", alignItems: "center", mr: 1, flexShrink: 0 }}>
        <Tooltip
          title={
            enabled
              ? "Auto-apply is ON — the firewall is kept on the best profile. Click for status."
              : "Auto-apply is OFF — the best profile is tracked but never applied for you. Click for history."
          }
        >
          <IconButton size="small" onClick={(e) => openPopover(e.currentTarget)}>
            <EmojiEventsIcon fontSize="small" color={enabled ? "warning" : "disabled"} />
          </IconButton>
        </Tooltip>
        <Typography
          variant="caption"
          color={enabled ? "text.primary" : "text.secondary"}
          sx={{
            display: { xs: "none", sm: "block" },
            cursor: "pointer",
            userSelect: "none",
            whiteSpace: "nowrap",
          }}
          onClick={(e) => openPopover(e.currentTarget as HTMLElement)}
        >
          Auto-apply best
        </Typography>
        <Tooltip title="Keep the firewall on the best profile automatically, switching when the ranking changes">
          <span>
            {/* On phones the toolbar is tight (menu, title, update button, jobs) — collapse
                to the trophy icon alone and let the popover carry the switch, so the
                "Update now" button is never crowded off the right edge. */}
            <Switch
              size="small"
              checked={enabled}
              disabled={busy || info === null}
              onChange={(e) => toggle(e.target.checked)}
              inputProps={{ "aria-label": "Auto-apply best" }}
              sx={{ display: { xs: "none", sm: "inline-flex" } }}
            />
          </span>
        </Tooltip>
      </Box>

      <Popover
        open={!!anchor}
        anchorEl={anchor}
        onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
        transformOrigin={{ vertical: "top", horizontal: "right" }}
        slotProps={{ paper: { sx: { p: 2, width: 360, maxWidth: "95vw" } } }}
      >
        <Stack spacing={1}>
          <Stack direction="row" alignItems="center" spacing={1}>
            <EmojiEventsIcon fontSize="small" color={enabled ? "warning" : "disabled"} />
            <Typography variant="subtitle2" sx={{ flexGrow: 1 }}>
              Auto-apply best
            </Typography>
            <Chip
              size="small"
              label={enabled ? "applying" : "tracking only"}
              color={enabled ? "warning" : "default"}
              variant="outlined"
            />
            {/* The popover always carries the switch — on phones it's the only one (the
                toolbar switch is hidden on xs to keep the update button visible). */}
            <Switch
              size="small"
              checked={enabled}
              disabled={busy || info === null}
              onChange={(e) => toggle(e.target.checked)}
              inputProps={{ "aria-label": "Auto-apply best" }}
            />
          </Stack>
          <Typography variant="caption" color="text.secondary">
            When on, the firewall is switched to whichever profile the chosen ranking names best.
            Re-checked after every run, with a full check every{" "}
            {Math.round(((info?.config.interval_minutes ?? 360) / 60) * 10) / 10} h.
          </Typography>

          {/* The first-class CROWNING POLICY: which verdict the follower acts on. Pick one;
              only that ranking's own settings show under it; Apply writes the whole draft.
              The pooled crown is always computed and shown below either way. */}
          {judged && judged.source === "methodology" ? (
            <MethodologyJudges j={judged} />
          ) : (
          <Box>
            <Typography variant="caption" color="text.secondary">
              Which ranking to follow
            </Typography>
            <Stack sx={{ mt: 0.5 }} spacing={0.25} role="radiogroup" aria-label="Which ranking to follow">
              {RANKINGS.map((r) => {
                const chosen = draft.policy === r.policy;
                return (
                  <Box
                    key={r.policy}
                    sx={{
                      borderRadius: 1,
                      border: 1,
                      borderColor: chosen ? "warning.main" : "transparent",
                      bgcolor: chosen ? "action.selected" : "transparent",
                    }}
                  >
                    <Stack
                      direction="row"
                      alignItems="flex-start"
                      spacing={0.5}
                      onClick={() => !busy && edit({ policy: r.policy })}
                      sx={{ cursor: busy ? "default" : "pointer", pr: 1 }}
                    >
                      <Radio
                        size="small"
                        checked={chosen}
                        disabled={busy}
                        onChange={() => edit({ policy: r.policy })}
                        inputProps={{ "aria-label": r.label }}
                        sx={{ p: 0.5 }}
                      />
                      <Box sx={{ py: 0.5 }}>
                        <Typography variant="body2" sx={{ lineHeight: 1.3 }}>
                          {r.label}
                          {policy === r.policy && (
                            <Typography component="span" variant="caption" color="text.secondary">
                              {" "}· current
                            </Typography>
                          )}
                        </Typography>
                        <Typography variant="caption" color="text.secondary" sx={{ display: "block", lineHeight: 1.3 }}>
                          {r.blurb}
                        </Typography>
                      </Box>
                    </Stack>
                    <Collapse in={chosen} unmountOnExit>
                      <Box sx={{ pl: 4.5, pr: 1, pb: 1 }}>
                        {(r.policy === "lcp_first" || r.policy === "fcp_first") && (
                          <BandField
                            leg={r.policy === "lcp_first" ? "lcp" : "fcp"}
                            value={r.policy === "lcp_first" ? draft.lcpBand : draft.fcpBand}
                            onChange={(v) => edit(r.policy === "lcp_first" ? { lcpBand: v } : { fcpBand: v })}
                            disabled={busy}
                          />
                        )}
                        {r.policy === "duel" &&
                          (info?.duel_champion ? (
                            <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                              Current champion:{" "}
                              <b>
                                {info.duel_champion.name ||
                                  info.duel_champion.label ||
                                  info.duel_champion.fingerprint}
                              </b>
                              {info.duel_champion.decisive ? "" : " (by draws only)"} · duel #
                              {info.duel_champion.duel_id}
                              {!info.duel_champion.decisive
                                ? ". Not decisive, so the pooled crown is followed instead."
                                : ""}
                              <FastestGapLine
                                gaps={legGaps.gaps[info.duel_champion.fingerprint]}
                                fastest={legGaps.fastest}
                              />
                            </Typography>
                          ) : (
                            <Typography variant="caption" color="text.disabled" sx={{ display: "block" }}>
                              No fresh duel verdict yet, so the pooled crown is followed instead.
                            </Typography>
                          ))}
                        {r.policy !== "lcp_first" && r.policy !== "fcp_first" && r.policy !== "duel" && (
                          <Typography variant="caption" color="text.disabled" sx={{ display: "block" }}>
                            No extra settings for this ranking.
                          </Typography>
                        )}
                      </Box>
                    </Collapse>
                  </Box>
                );
              })}
            </Stack>

            {/* The speed budget guards whichever ranking is chosen, so it sits under the
                list rather than inside one option. */}
            <Tooltip title="Auto-apply will not follow a profile that is clearly slower than the field's fastest LCP or FCP by more than this; it follows the first ranking whose pick fits and says so. Gaps inside the measurement noise never count.">
              <Box sx={{ mt: 1 }}>
                <Stack direction="row" spacing={1} alignItems="center">
                  <Switch
                    size="small"
                    checked={draft.budgetOn}
                    onChange={(e) => edit({ budgetOn: e.target.checked })}
                    disabled={busy}
                    inputProps={{ "aria-label": "Speed budget" }}
                  />
                  <Typography variant="caption" color="text.secondary">
                    Speed budget (applies to any ranking)
                  </Typography>
                </Stack>
                <Collapse in={draft.budgetOn} unmountOnExit>
                  <Stack direction="row" spacing={1} alignItems="center" sx={{ pl: 1 }} flexWrap="wrap" useFlexGap>
                    <Typography variant="caption" color="text.secondary">
                      At most
                    </Typography>
                    <PctField value={draft.budgetLcp} onChange={(v) => edit({ budgetLcp: v })} disabled={busy} max={200} label="LCP budget" />
                    <Typography variant="caption" color="text.secondary">
                      % slower LCP and
                    </Typography>
                    <PctField value={draft.budgetFcp} onChange={(v) => edit({ budgetFcp: v })} disabled={busy} max={200} label="FCP budget" />
                    <Typography variant="caption" color="text.secondary">
                      % slower FCP than the fastest
                    </Typography>
                  </Stack>
                </Collapse>
              </Box>
            </Tooltip>

            <Stack direction="row" spacing={1} justifyContent="flex-end" alignItems="center" sx={{ mt: 1 }}>
              {dirty && (
                <Typography variant="caption" color="warning.main" sx={{ flexGrow: 1 }}>
                  Not applied yet
                </Typography>
              )}
              <Button size="small" onClick={() => setDraft(saved)} disabled={busy || !dirty}>
                Discard
              </Button>
              <Button
                size="small"
                variant="contained"
                color="warning"
                onClick={apply}
                disabled={busy || !dirty}
                startIcon={busy && dirty ? <CircularProgress size={12} /> : undefined}
              >
                Apply
              </Button>
            </Stack>
          </Box>
          )}

          <Divider />

          {/* What the follower actually WRITES is the governing crown — the one the crowning
              policy resolved. This block used to render the pooled crown under the heading
              "Crown", with the on-crown chip beside it, while `policy = "duel"` was applying
              the duel champion: the popover named one profile and the firewall was on
              another, and the chip belonged to neither line. The pooled crown is still
              shown, demoted, because it is the tracked statistic the churn stats count. */}
          <Box>
            <Typography variant="caption" color="text.secondary">
              {followedSource === "ring"
                ? "Following · the ring's champion"
                : followedSource === "fused"
                ? "Following · Overall ranking"
                : followedSource === "duel"
                  ? "Following · duel champion"
                  : followedSource === "math"
                    ? "Following · fastest on paper (FCP × LCP)"
                    : followedSource === "lcp_first"
                      ? "Following · LCP first"
                      : followedSource === "fcp_first"
                        ? "Following · FCP first"
                      : policy === "lcp_first" || policy === "fcp_first"
                        ? "Following · pooled crown (no confident FCP and LCP reading yet)"
                    : policy === "math"
                      ? "Following · pooled crown (no confident FCP × LCP reading yet)"
                  : policy === "duel"
                    ? "Following · pooled crown (no fresh duel verdict)"
                    : policy === "fused"
                      ? "Following · pooled crown (the Overall ranking names nobody yet)"
                      : "Following · pooled crown"}
            </Typography>
            {followedFp ? (
              <>
                <Typography variant="body2">
                  <Link
                    component={RouterLink}
                    to={`/profiles/${encodeURIComponent(followedFp)}`}
                    onClick={() => setAnchor(null)}
                   
                  >
                    {followedName}
                  </Link>{" "}
                  {/* The chip belongs to the FOLLOWED profile — `on_crown` is computed
                      against the governing target, so pinning it to the pooled crown (as
                      it was) described a profile it wasn't measuring. */}
                  {last?.on_crown === true && (
                    <Chip size="small" label="on the firewall" color="success" variant="outlined" />
                  )}
                  {last?.on_crown === false && (
                    <Chip size="small" label="firewall elsewhere" color="warning" variant="outlined" />
                  )}
                </Typography>
                <FastestGapLine gaps={followedFp ? legGaps.gaps[followedFp] : null} fastest={legGaps.fastest} />
                {/* The settings that name stands for — kept, demoted, clamped. A summary is
                    what you read once you already know which profile you are looking at. */}
                {followedName !== followedLabel && followedLabel && (
                  <Typography
                    variant="caption"
                    color="text.disabled"
                    title={followedLabel}
                    sx={{
                      display: "-webkit-box",
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: "vertical",
                      overflow: "hidden",
                    }}
                  >
                    {followedLabel}
                  </Typography>
                )}
                {/* The other verdict, when it names someone else. Saying it outright is the
                    point: the churn stats and the change list below are all about THIS
                    profile, which is not the one on the firewall. */}
                {pooledDiffers && (
                  <Typography variant="caption" color="warning.main" display="block">
                    Pooled crown is{" "}
                    {last?.crown_name || last?.crown_label || last?.crown_fingerprint} — tracked
                    below, not followed.
                  </Typography>
                )}
              </>
            ) : (
              <Typography variant="body2" color="text.secondary">
                {stats?.current_crown_name ||
                  stats?.current_crown_label ||
                  stats?.current_crown_fingerprint ||
                  "No confident crown yet"}
              </Typography>
            )}
            <Typography variant="caption" color="text.disabled">
              Last check: {info?.status.last_check_at ? fmtTimeShort(info.status.last_check_at) : "—"}
              {last?.apply_skipped ? ` · ${last.apply_skipped}` : ""}
              {last?.error ? ` · ${last.error}` : ""}
            </Typography>
          </Box>

          {/* Churn stats: how often does the best profile change? */}
          <Box>
            <Typography variant="caption" color="text.secondary">
              Crown stability
            </Typography>
            {stats && stats.tracked_since ? (
              <>
                <Typography variant="body2">
                  Changed <b>{stats.changes_7d}×</b> in 7 days ({stats.changes_24h}× in 24 h,{" "}
                  {stats.changes_30d}× in 30 days)
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  Median reign {fmtHours(stats.median_reign_hours)} · current reign{" "}
                  {fmtHours(stats.current_reign_hours)}
                  {stats.changes_per_day != null ? ` · ${stats.changes_per_day}/day` : ""} · tracked
                  since {fmtTimeShort(stats.tracked_since)}
                </Typography>
              </>
            ) : (
              <Typography variant="body2" color="text.secondary">
                No crown observations yet — stats appear after the first check.
              </Typography>
            )}
          </Box>

          {/* Recent crown changes */}
          {changes.length > 0 && (
            <Box>
              <Typography variant="caption" color="text.secondary">
                Recent changes
              </Typography>
              <Stack spacing={0.5} sx={{ mt: 0.5 }}>
                {changes.map((e) => (
                  <Typography key={e.id} variant="caption" sx={{ display: "block" }}>
                    {fmtTimeShort(e.created_at)} ·{" "}
                    {e.previous_fingerprint
                      ? `${e.previous_name || e.previous_label || e.previous_fingerprint} → ${
                          e.name || e.label || e.fingerprint
                        }`
                      : `tracking started (${e.name || e.label || e.fingerprint})`}
                    {e.applied && (
                      <Chip
                        size="small"
                        label="applied"
                        color="success"
                        variant="outlined"
                        sx={{ ml: 0.5, height: 16, fontSize: "0.65rem" }}
                      />
                    )}
                    {e.error && (
                      <Chip
                        size="small"
                        label="apply failed"
                        color="error"
                        variant="outlined"
                        sx={{ ml: 0.5, height: 16, fontSize: "0.65rem" }}
                      />
                    )}
                  </Typography>
                ))}
              </Stack>
            </Box>
          )}

          <Stack direction="row" spacing={1} justifyContent="flex-end">
            <Button
              size="small"
              onClick={syncNow}
              disabled={busy}
              startIcon={busy ? <CircularProgress size={12} /> : undefined}
            >
              Check now
            </Button>
          </Stack>
        </Stack>
      </Popover>

      <Snackbar
        open={!!snack}
        autoHideDuration={6000}
        onClose={() => setSnack(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        {snack ? (
          <Alert severity={snack.sev} variant="filled" onClose={() => setSnack(null)}>
            {snack.msg}
          </Alert>
        ) : undefined}
      </Snackbar>
    </>
  );
}

// ── Ranking choices and the draft ──────────────────────────────────────────────────

/** Who decides the crown, when the methodology states it: read-only, because changing it
 * is publishing a new methodology, not a setting. */
function MethodologyJudges({ j }: { j: CrownJudgement }) {
  const short = j.version.replace(/^speed-smoothness-/, "");
  const b = j.speed_budget;
  return (
    <Box>
      <Typography variant="caption" color="text.secondary">
        Who decides the crown · methodology {short}
      </Typography>
      <Typography variant="body2" sx={{ mt: 0.5 }}>
        {j.crowned_by === "ring"
          ? `The ring's champion (${j.ring_crown_rule} rule${j.ring_min_margin > 0 ? `, wins by ${j.ring_min_margin} pts or more` : ", a win is a win"}). A draw leaves the crown where it is.`
          : "The crowning policy the methodology names."}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
        {b.enabled
          ? `Kept within the speed budget: at most ${b.lcp_pct}% slower LCP and ${b.fcp_pct}% slower FCP than a profile it raced, judged on ${j.budget_judged_on === "ring" ? "head-to-head milliseconds" : "the pooled record"}.`
          : "No speed budget."}{" "}
        {j.allow_unshaped_crown ? "SQM off may hold the crown." : "SQM off races but may not hold the crown."}
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
        These are part of the methodology, so changing them means publishing a new version.
      </Typography>
    </Box>
  );
}

const RANKINGS: { policy: CrowningPolicy; label: string; blurb: string }[] = [
  {
    policy: "fused",
    label: "Overall (both records)",
    blurb: "Pooled results and head-to-head duels fitted together. The default.",
  },
  { policy: "pooled", label: "Pooled Overall", blurb: "The all-time best by average across every run." },
  {
    policy: "duel",
    label: "Duel champion",
    blurb: "The latest head-to-head winner, measured in the same weather.",
  },
  {
    policy: "math",
    label: "Fastest on paper",
    blurb: "Lowest FCP × LCP in milliseconds. Can switch on small differences.",
  },
  {
    policy: "lcp_first",
    label: "LCP first",
    blurb: "Among profiles close to the best LCP, the one with the fastest FCP.",
  },
  {
    policy: "fcp_first",
    label: "FCP first",
    blurb: "Among profiles close to the best FCP, the one with the fastest LCP.",
  },
];

type Draft = {
  policy: CrowningPolicy;
  lcpBand: string;
  fcpBand: string;
  budgetOn: boolean;
  budgetLcp: string;
  budgetFcp: string;
};

function draftFrom(info: CrownFollowStatus | null): Draft {
  const budget = info?.config.speed_budget ?? { enabled: true, lcp_pct: 20, fcp_pct: 25 };
  return {
    policy: info?.config.policy ?? "fused",
    lcpBand: String(info?.config.lcp_band_pct ?? 10),
    fcpBand: String(info?.config.fcp_band_pct ?? 10),
    budgetOn: budget.enabled,
    budgetLcp: String(budget.lcp_pct),
    budgetFcp: String(budget.fcp_pct),
  };
}

function sameDraft(a: Draft, b: Draft): boolean {
  return (
    a.policy === b.policy &&
    a.budgetOn === b.budgetOn &&
    Number(a.lcpBand) === Number(b.lcpBand) &&
    Number(a.fcpBand) === Number(b.fcpBand) &&
    Number(a.budgetLcp) === Number(b.budgetLcp) &&
    Number(a.budgetFcp) === Number(b.budgetFcp)
  );
}

function validateDraft(d: Draft): string | null {
  const inRange = (v: string, max: number) => {
    const n = Number(v);
    return v.trim() !== "" && Number.isFinite(n) && n >= 0 && n <= max;
  };
  if (d.policy === "lcp_first" && !inRange(d.lcpBand, 50)) return "The LCP band must be between 0 and 50%.";
  if (d.policy === "fcp_first" && !inRange(d.fcpBand, 50)) return "The FCP band must be between 0 and 50%.";
  if (d.budgetOn && (!inRange(d.budgetLcp, 200) || !inRange(d.budgetFcp, 200)))
    return "A speed budget must be between 0 and 200%.";
  return null;
}

function PctField(props: {
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
  max: number;
  label: string;
}) {
  return (
    <TextField
      size="small"
      type="number"
      value={props.value}
      onChange={(e) => props.onChange(e.target.value)}
      disabled={props.disabled}
      inputProps={{
        min: 0,
        max: props.max,
        step: 1,
        "aria-label": props.label,
        style: { width: 44, padding: "2px 6px", fontSize: 12 },
      }}
    />
  );
}

function BandField(props: {
  leg: "lcp" | "fcp";
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
}) {
  const leg = props.leg.toUpperCase();
  const other = props.leg === "lcp" ? "FCP" : "LCP";
  return (
    <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
      <Typography variant="caption" color="text.secondary">
        Close means within
      </Typography>
      <PctField value={props.value} onChange={props.onChange} disabled={props.disabled} max={50} label={`${leg} band`} />
      <Typography variant="caption" color="text.secondary">
        % of the best {leg}, traded for a faster {other}.
      </Typography>
    </Stack>
  );
}

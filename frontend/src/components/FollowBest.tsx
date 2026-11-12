// Top-bar "Auto-apply best" control: a switch that arms the crown follower (keep the
// firewall's SQM settings on the best profile as the ranking changes), plus a popover that
// answers three questions in order:
//   1. What is being followed right now, and if the speed budget passed the chosen
//      ranking's pick over, which one and why.
//   2. Which ranking to follow — every ranking is a row naming the profile it would pick,
//      that pick's gap to the fastest LCP and FCP, and whether it is over the speed budget.
//      Only the selected row shows its own setting (the band of LCP first / FCP first).
//   3. The speed budget, whose percentages show only while it is on.
// Crown history (churn stats, recent changes) sits last, folded. Tracking runs whether or
// not the switch is on; the switch only arms the firewall write.
import { useCallback, useEffect, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import ButtonBase from "@mui/material/ButtonBase";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Collapse from "@mui/material/Collapse";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import InputAdornment from "@mui/material/InputAdornment";
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
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";

import FastestGapLine from "./FastestGapLine";
import { useFastestGaps } from "../hooks/useFastestGaps";
import { api } from "../api/client";
import type {
  CrownFollowStatus,
  CrowningPolicy,
  CrownsOut,
  LegGap,
  LegGaps,
  SpeedBudget,
} from "../api/types";
import { fmtTimeShort } from "../utils/format";

const POLL_MS = 60_000;

function fmtHours(h: number | null | undefined): string {
  if (h == null || Number.isNaN(h)) return "—";
  if (h < 1) return `${Math.round(h * 60)} min`;
  if (h < 48) return `${h.toFixed(h < 10 ? 1 : 0)} h`;
  return `${(h / 24).toFixed(1)} d`;
}

// Every ranking Auto-apply can follow, in the order a reader should meet them.
const RANKINGS: { key: CrowningPolicy; title: string; blurb: string }[] = [
  {
    key: "fused",
    title: "Overall (both records)",
    blurb:
      "One ranking fitted over both records: each profile's pooled median, corrected by every head-to-head round it fought. The default.",
  },
  {
    key: "lcp_first",
    title: "LCP first",
    blurb:
      "Every profile within the band of the best LCP qualifies; the one with the fastest FCP among them wins.",
  },
  {
    key: "fcp_first",
    title: "FCP first",
    blurb:
      "Every profile within the band of the best FCP qualifies; the one with the fastest LCP among them wins.",
  },
  {
    key: "pooled",
    title: "Pooled Overall",
    blurb: "The all-time best by average over every run, weather averaged out.",
  },
  {
    key: "duel",
    title: "Duel champion",
    blurb:
      "The duel ladder's head-to-head champion. Falls back to the pooled crown when no decisive, fresh verdict exists.",
  },
  {
    key: "math",
    title: "Fastest on paper",
    blurb:
      "Lowest median FCP × LCP in raw milliseconds. Ignores smoothness and has no tie test, so it can switch on small differences.",
  },
];

const titleOf = (k: string | null | undefined) =>
  RANKINGS.find((r) => r.key === k)?.title ?? (k === "pooled" ? "Pooled Overall" : k ?? "");

type Pick = { fingerprint: string; name: string | null; gaps: LegGaps | null; note?: string };

// The profile a ranking would pick, read off the crowns endpoint.
function pickOf(crowns: CrownsOut | null, key: CrowningPolicy): Pick | null {
  if (!crowns) return null;
  const c =
    key === "fused"
      ? crowns.fused
      : key === "pooled"
        ? crowns.pooled
        : key === "duel"
          ? crowns.duel
          : key === "math"
            ? crowns.math
            : key === "lcp_first"
              ? crowns.lcp_first
              : crowns.fcp_first;
  if (!c || !c.fingerprint) return null;
  let note: string | undefined;
  if (key === "duel" && crowns.duel) {
    if (!crowns.duel.decisive) note = "draws only, so the pooled crown is followed";
    else if (!crowns.duel.fresh) note = "verdict expired, so the pooled crown is followed";
  }
  return {
    fingerprint: c.fingerprint,
    name: c.name || c.label || c.fingerprint,
    gaps: c.vs_fastest ?? null,
    note,
  };
}

// The legs on which a pick is clearly over the budget: the same rule the server applies
// (a gap counts only when it clears the noise bar).
function overLegs(gaps: LegGaps | null, budget: SpeedBudget): { leg: string; pct: number; limit: number }[] {
  if (!gaps || !budget.enabled) return [];
  const out: { leg: string; pct: number; limit: number }[] = [];
  const check = (leg: string, g: LegGap | undefined, limit: number) => {
    if (g && g.clear && g.pct > limit) out.push({ leg, pct: g.pct, limit });
  };
  check("LCP", gaps.lcp, budget.lcp_pct);
  check("FCP", gaps.fcp, budget.fcp_pct);
  return out;
}

// One leg's gap in a few characters, for the ranking rows. The full sentence (with
// milliseconds and the noise bar) is the FastestGapLine under "Following now".
function shortLeg(name: string, g?: LegGap): string {
  if (!g) return `${name} —`;
  if (g.is_fastest) return `${name} fastest`;
  if (!g.clear) return `${name} ≈ fastest`;
  return `${name} ${g.pct > 0 ? "+" : "−"}${Math.abs(g.pct).toFixed(0)}%`;
}

export default function FollowBest() {
  const [info, setInfo] = useState<CrownFollowStatus | null>(null);
  const [crowns, setCrowns] = useState<CrownsOut | null>(null);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [busy, setBusy] = useState(false); // a save or a check in flight
  const [showHistory, setShowHistory] = useState(false);
  const [snack, setSnack] = useState<{ msg: string; sev: "success" | "error" | "info" } | null>(
    null,
  );

  const load = useCallback(() => {
    api
      .crownFollow()
      .then(setInfo)
      .catch(() => {});
  }, []);
  // The crowns cost more than the follower status, so they are read only while the
  // popover is open (and again after anything in it changes).
  const loadCrowns = useCallback(() => {
    api
      .crowns()
      .then(setCrowns)
      .catch(() => {});
  }, []);
  const reload = useCallback(() => {
    load();
    if (anchor) loadCrowns();
  }, [load, loadCrowns, anchor]);

  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load]);
  useEffect(() => {
    if (anchor) loadCrowns();
  }, [anchor, loadCrowns]);

  const enabled = info?.config.enabled ?? false;
  const policy = info?.config.policy ?? "fused";

  // The two leg-first crowns' bands — one stated trade each, edited the same way.
  const bands = {
    lcp: info?.config.lcp_band_pct ?? 10,
    fcp: info?.config.fcp_band_pct ?? 10,
  };
  const [bandDraft, setBandDraft] = useState<{ lcp: string; fcp: string }>({
    lcp: String(bands.lcp),
    fcp: String(bands.fcp),
  });
  useEffect(
    () => setBandDraft({ lcp: String(bands.lcp), fcp: String(bands.fcp) }),
    [bands.lcp, bands.fcp],
  );

  const saveBand = (leg: "lcp" | "fcp") => {
    const v = Number(bandDraft[leg]);
    const current = bands[leg];
    const reset = () => setBandDraft((d) => ({ ...d, [leg]: String(current) }));
    if (!Number.isFinite(v) || v === current) {
      reset();
      return;
    }
    if (v < 0 || v > 50) {
      setSnack({ msg: `The ${leg.toUpperCase()} band must be between 0 and 50%.`, sev: "error" });
      reset();
      return;
    }
    setBusy(true);
    api
      .crownFollowUpdate(leg === "lcp" ? { lcp_band_pct: v } : { fcp_band_pct: v })
      .then(() => {
        setSnack({
          msg: `${leg.toUpperCase()} first now accepts up to ${v}% behind the best ${leg.toUpperCase()}.`,
          sev: "info",
        });
        reload();
      })
      .catch((e) => setSnack({ msg: e instanceof Error ? e.message : "Could not save the band.", sev: "error" }))
      .finally(() => setBusy(false));
  };

  // The speed budget: how much slower than the fastest LCP / FCP a followed crown may be.
  const budget: SpeedBudget = info?.config.speed_budget ?? { enabled: true, lcp_pct: 20, fcp_pct: 25 };
  const [budgetDraft, setBudgetDraft] = useState({ lcp: String(budget.lcp_pct), fcp: String(budget.fcp_pct) });
  useEffect(
    () => setBudgetDraft({ lcp: String(budget.lcp_pct), fcp: String(budget.fcp_pct) }),
    [budget.lcp_pct, budget.fcp_pct],
  );
  const saveBudget = (patch: { enabled?: boolean; lcp_pct?: number; fcp_pct?: number }) => {
    for (const v of [patch.lcp_pct, patch.fcp_pct]) {
      if (v !== undefined && (!Number.isFinite(v) || v < 0 || v > 200)) {
        setSnack({ msg: "A speed budget must be between 0 and 200%.", sev: "error" });
        setBudgetDraft({ lcp: String(budget.lcp_pct), fcp: String(budget.fcp_pct) });
        return;
      }
    }
    setBusy(true);
    api
      .crownFollowUpdate({ speed_budget: patch })
      .then(() => {
        setSnack({ msg: "Speed budget saved.", sev: "info" });
        reload();
      })
      .catch((e) => setSnack({ msg: e instanceof Error ? e.message : "Could not save the budget.", sev: "error" }))
      .finally(() => setBusy(false));
  };

  const setPolicy = (next: CrowningPolicy) => {
    if (next === policy) return;
    setBusy(true);
    setInfo((v) => (v ? { ...v, config: { ...v.config, policy: next } } : v));
    api
      .crownFollowUpdate({ policy: next })
      .then(() => {
        setSnack({ msg: `Auto-apply now follows ${titleOf(next)}.`, sev: "info" });
        reload();
      })
      .catch((e) =>
        setSnack({ msg: e instanceof Error ? e.message : "Could not change the ranking.", sev: "error" }),
      )
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
            ? { msg: "Auto-apply is on — the firewall will be kept on the best profile.", sev: "success" }
            : { msg: "Auto-apply is off — changes of the best profile are still recorded.", sev: "info" },
        );
        reload();
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
        if (result.applied) setSnack({ msg: "Applied to the firewall.", sev: "success" });
        else if (result.error) setSnack({ msg: result.error, sev: "error" });
        else if (result.apply_skipped) setSnack({ msg: result.apply_skipped, sev: "info" });
        else if (result.on_crown) setSnack({ msg: "The firewall is already on the followed profile.", sev: "success" });
        else if (!result.crown_fingerprint) setSnack({ msg: "No confident crown yet — nothing to follow.", sev: "info" });
        else setSnack({ msg: "Checked.", sev: "info" });
        reload();
      })
      .catch((e) => setSnack({ msg: e instanceof Error ? e.message : "Check failed.", sev: "error" }))
      .finally(() => setBusy(false));
  };

  const stats = info?.stats;
  const last = info?.status.last_result;

  // What is followed. The crowns endpoint resolves it live (policy + speed budget); the
  // follower's last check is the fallback until that has loaded.
  const followedFp =
    crowns?.governing.fingerprint ?? last?.governing_fingerprint ?? last?.crown_fingerprint ?? null;
  const followedSource = crowns?.governing.source ?? last?.governing_source ?? null;
  const picks = Object.fromEntries(RANKINGS.map((r) => [r.key, pickOf(crowns, r.key)])) as Record<
    CrowningPolicy,
    Pick | null
  >;
  const followedPick = Object.values(picks).find((p) => p && p.fingerprint === followedFp) ?? null;
  const followedName =
    followedPick?.name ||
    (followedFp === last?.governing_fingerprint ? last?.governing_name || last?.governing_label : null) ||
    followedFp;
  // Gaps for the followed profile: off the crowns when it is one of them, else asked for.
  const fetched = useFastestGaps(followedPick ? [] : [followedFp]);
  const followedGaps = followedPick?.gaps ?? (followedFp ? fetched.gaps[followedFp] : null);
  const fastest = crowns?.fastest ?? fetched.fastest;

  const sb = crowns?.governing.speed_budget;
  const policyPick = picks[policy];
  const replaced = sb?.replaced ? policyPick : null;
  const noneFits = !!(sb?.checked && sb.within === false && !sb.replaced);
  const changes = (info?.events ?? []).filter((e) => e.kind === "change").slice(0, 5);

  const legPhrase = (o: { leg: string; pct: number; limit: number }[]) =>
    o.map((x) => `${x.leg} is ${x.pct.toFixed(0)}% slower than the fastest (budget ${x.limit}%)`).join(" and ");

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
          <IconButton size="small" onClick={(e) => setAnchor(e.currentTarget)} aria-label="Auto-apply status">
            <EmojiEventsIcon fontSize="small" color={enabled ? "warning" : "disabled"} />
          </IconButton>
        </Tooltip>
        <Typography
          variant="caption"
          color={enabled ? "text.primary" : "text.secondary"}
          sx={{ display: { xs: "none", sm: "block" }, cursor: "pointer", userSelect: "none", whiteSpace: "nowrap" }}
          onClick={(e) => setAnchor(e.currentTarget as HTMLElement)}
        >
          Auto-apply best
        </Typography>
        <Tooltip title="Keep the firewall on the best profile automatically, switching when the ranking changes">
          <span>
            {/* On phones the toolbar is tight — the trophy alone, and the popover carries
                the switch. */}
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
        slotProps={{ paper: { sx: { p: 2, width: 400, maxWidth: "calc(100vw - 16px)" } } }}
      >
        <Stack spacing={3}>
          {/* ── Header: the switch ─────────────────────────────────────────────── */}
          <Stack spacing={0.5}>
            <Stack direction="row" alignItems="center" spacing={1}>
              <EmojiEventsIcon color={enabled ? "warning" : "disabled"} />
              <Typography variant="h6" sx={{ flexGrow: 1, fontSize: "1.125rem", letterSpacing: "-0.01em" }}>
                Auto-apply best
              </Typography>
              <Switch
                checked={enabled}
                disabled={busy || info === null}
                onChange={(e) => toggle(e.target.checked)}
                inputProps={{ "aria-label": "Auto-apply best" }}
              />
            </Stack>
            <Typography variant="body2" color="text.secondary" sx={{ lineHeight: 1.5 }}>
              {enabled
                ? "On: the firewall is switched to whatever the chosen ranking names best."
                : "Off: the best profile is tracked, but nothing is written to the firewall."}
            </Typography>
          </Stack>

          {/* ── Following now: the one answer that matters, first ─────────────── */}
          <Stack spacing={1}>
            <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1.5 }}>
              {enabled ? "Following now" : "Would follow"}
              {followedSource ? ` · ${titleOf(followedSource)}` : ""}
            </Typography>
            {followedFp ? (
              <Box>
                <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                  <Link
                    component={RouterLink}
                    to={`/profiles/${encodeURIComponent(followedFp)}`}
                    onClick={() => setAnchor(null)}
                    underline="always"
                    variant="body1"
                    sx={{ fontWeight: 600 }}
                  >
                    {followedName}
                  </Link>
                  {last?.on_crown === true && (
                    <Chip size="small" label="on the firewall" color="success" variant="outlined" />
                  )}
                  {last?.on_crown === false && (
                    <Chip size="small" label="firewall is elsewhere" color="warning" variant="outlined" />
                  )}
                </Stack>
                <FastestGapLine gaps={followedGaps} fastest={fastest} />
              </Box>
            ) : (
              <Typography variant="body2" color="text.secondary">
                No confident crown yet — nothing to follow.
              </Typography>
            )}
            {replaced && sb?.replaced && (
              <Alert severity="warning" icon={<WarningAmberIcon fontSize="inherit" />} sx={{ py: 0.5 }}>
                {titleOf(policy)} picks <b>{replaced.name}</b>, but its{" "}
                {legPhrase(overLegs(replaced.gaps, budget)) || "speed is over the budget"}. Following{" "}
                <b>{followedName}</b> ({titleOf(sb.replaced.source)}) instead — the first ranking whose
                pick fits.
              </Alert>
            )}
            {noneFits && (
              <Alert severity="warning" icon={<WarningAmberIcon fontSize="inherit" />} sx={{ py: 0.5 }}>
                Every ranking's pick is over the speed budget, so {titleOf(policy)}'s pick is followed
                anyway. Loosen the budget below, or measure faster profiles.
              </Alert>
            )}
            <Typography variant="caption" color="text.secondary">
              Last check {info?.status.last_check_at ? fmtTimeShort(info.status.last_check_at) : "not run yet"}
              {last?.apply_skipped ? ` · ${last.apply_skipped}` : ""}
              {last?.error ? ` · ${last.error}` : ""}
            </Typography>
          </Stack>

          {/* ── Which ranking: each row names its pick ─────────────────────────── */}
          <Stack spacing={1} role="radiogroup" aria-label="Which ranking to follow">
            <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1.5 }}>
              Which ranking to follow
            </Typography>
            {RANKINGS.map((r) => {
              const pick = picks[r.key];
              const selected = policy === r.key;
              const over = overLegs(pick?.gaps ?? null, budget);
              const band = r.key === "lcp_first" ? "lcp" : r.key === "fcp_first" ? "fcp" : null;
              return (
                <Box
                  key={r.key}
                  sx={{
                    borderRadius: 1,
                    border: 1,
                    borderColor: selected ? "warning.main" : "divider",
                    bgcolor: selected ? "action.selected" : "transparent",
                  }}
                >
                  <ButtonBase
                    role="radio"
                    aria-checked={selected}
                    onClick={() => setPolicy(r.key)}
                    disabled={busy}
                    sx={{ width: "100%", textAlign: "left", justifyContent: "flex-start", alignItems: "flex-start", p: 1, minHeight: 48, borderRadius: 1 }}
                  >
                    <Radio
                      checked={selected}
                      size="small"
                      tabIndex={-1}
                      disableRipple
                      color="warning"
                      sx={{ p: 0.5, mr: 1 }}
                    />
                    <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                      <Typography variant="body2" sx={{ fontWeight: 600 }}>
                        {r.title}
                      </Typography>
                      <Typography variant="body2" color="text.secondary" noWrap>
                        {crowns === null ? "…" : pick ? pick.name : "No confident pick yet"}
                      </Typography>
                      {pick && (
                        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                          <Typography
                            variant="caption"
                            color={over.length ? "warning.main" : "text.secondary"}
                            sx={{ fontVariantNumeric: "tabular-nums" }}
                          >
                            {shortLeg("LCP", pick.gaps?.lcp)} · {shortLeg("FCP", pick.gaps?.fcp)}
                          </Typography>
                          {over.length > 0 && (
                            <Chip
                              size="small"
                              icon={<WarningAmberIcon />}
                              label={`over budget (${over.map((o) => o.leg).join(", ")})`}
                              color="warning"
                              variant="outlined"
                              sx={{ height: 20 }}
                            />
                          )}
                          {pick.note && (
                            <Typography variant="caption" color="text.secondary">
                              {pick.note}
                            </Typography>
                          )}
                        </Stack>
                      )}
                    </Box>
                  </ButtonBase>
                  {/* Only the selected ranking shows what it is and its own setting. */}
                  <Collapse in={selected} unmountOnExit>
                    <Stack spacing={1} sx={{ px: 1, pb: 1, pl: 5 }}>
                      <Typography variant="caption" color="text.secondary" sx={{ lineHeight: 1.5 }}>
                        {r.blurb}
                      </Typography>
                      {band && (
                        <TextField
                          size="small"
                          label={`Band: up to this far behind the best ${band.toUpperCase()}`}
                          value={bandDraft[band]}
                          onChange={(e) => setBandDraft((d) => ({ ...d, [band]: e.target.value }))}
                          onBlur={() => saveBand(band)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") saveBand(band);
                          }}
                          disabled={busy}
                          inputProps={{ inputMode: "decimal", style: { fontSize: 16 } }}
                          InputProps={{ endAdornment: <InputAdornment position="end">%</InputAdornment> }}
                          sx={{ maxWidth: 280 }}
                        />
                      )}
                    </Stack>
                  </Collapse>
                </Box>
              );
            })}
          </Stack>

          {/* ── Speed budget: the percentages only while it is on ──────────────── */}
          <Stack spacing={1}>
            <Stack direction="row" alignItems="center" spacing={1}>
              <Box sx={{ flexGrow: 1 }}>
                <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1.5, display: "block" }}>
                  Speed budget
                </Typography>
                <Typography variant="body2" color="text.secondary" sx={{ lineHeight: 1.5 }}>
                  {budget.enabled
                    ? "Never follow a pick clearly slower than the fastest by more than:"
                    : "Off: any ranking's pick is followed, however slow."}
                </Typography>
              </Box>
              <Switch
                checked={budget.enabled}
                onChange={(e) => saveBudget({ enabled: e.target.checked })}
                disabled={busy}
                inputProps={{ "aria-label": "Speed budget" }}
              />
            </Stack>
            <Collapse in={budget.enabled} unmountOnExit>
              <Stack direction="row" spacing={2}>
                {(["lcp", "fcp"] as const).map((leg) => (
                  <TextField
                    key={leg}
                    size="small"
                    label={`${leg.toUpperCase()} slower by`}
                    value={budgetDraft[leg]}
                    onChange={(e) => setBudgetDraft((d) => ({ ...d, [leg]: e.target.value }))}
                    onBlur={() => {
                      const v = Number(budgetDraft[leg]);
                      const cur = leg === "lcp" ? budget.lcp_pct : budget.fcp_pct;
                      if (v !== cur) saveBudget(leg === "lcp" ? { lcp_pct: v } : { fcp_pct: v });
                    }}
                    disabled={busy}
                    inputProps={{ inputMode: "decimal", style: { fontSize: 16 } }}
                    InputProps={{ endAdornment: <InputAdornment position="end">%</InputAdornment> }}
                    sx={{ flex: 1 }}
                  />
                ))}
              </Stack>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1, lineHeight: 1.5 }}>
                A pick over budget is passed over for the first ranking that fits (LCP first, Overall, FCP
                first, pooled, duel, fastest on paper). Gaps inside the noise never count.
              </Typography>
            </Collapse>
          </Stack>

          <Divider />

          {/* ── History, folded ─────────────────────────────────────────────────── */}
          <Box>
            <ButtonBase
              onClick={() => setShowHistory((v) => !v)}
              aria-expanded={showHistory}
              sx={{ width: "100%", justifyContent: "space-between", minHeight: 44, borderRadius: 1 }}
            >
              <Box sx={{ textAlign: "left" }}>
                <Typography variant="body2" sx={{ fontWeight: 600 }}>
                  Crown history
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {stats && stats.tracked_since
                    ? `Changed ${stats.changes_7d}× in 7 days · current reign ${fmtHours(stats.current_reign_hours)}`
                    : "No crown observations yet"}
                </Typography>
              </Box>
              <ExpandMoreIcon
                sx={{ transform: showHistory ? "rotate(180deg)" : "none", transition: "transform 150ms" }}
              />
            </ButtonBase>
            <Collapse in={showHistory} unmountOnExit>
              <Stack spacing={1} sx={{ mt: 1 }}>
                {stats && stats.tracked_since && (
                  <Typography variant="caption" color="text.secondary" sx={{ lineHeight: 1.5 }}>
                    {stats.changes_24h}× in 24 h, {stats.changes_30d}× in 30 days · median reign{" "}
                    {fmtHours(stats.median_reign_hours)}
                    {stats.changes_per_day != null ? ` · ${stats.changes_per_day}/day` : ""} · tracked since{" "}
                    {fmtTimeShort(stats.tracked_since)}
                  </Typography>
                )}
                {changes.map((e) => (
                  <Typography key={e.id} variant="caption" sx={{ display: "block", lineHeight: 1.5 }}>
                    {fmtTimeShort(e.created_at)} ·{" "}
                    {e.previous_fingerprint
                      ? `${e.previous_name || e.previous_label || e.previous_fingerprint} → ${e.name || e.label || e.fingerprint}`
                      : `tracking started (${e.name || e.label || e.fingerprint})`}
                    {e.applied && " · applied"}
                    {e.error && " · apply failed"}
                  </Typography>
                ))}
              </Stack>
            </Collapse>
          </Box>

          {/* The one action, at the bottom where the thumb is. */}
          <Button
            variant="outlined"
            fullWidth
            onClick={syncNow}
            disabled={busy}
            startIcon={busy ? <CircularProgress size={14} /> : undefined}
            sx={{ minHeight: 44 }}
          >
            Check now
          </Button>
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

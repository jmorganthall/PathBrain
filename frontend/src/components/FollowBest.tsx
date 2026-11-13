// Top-bar "Follow best" control: a switch that arms the crown follower (keep the
// firewall's SQM settings on the crowned best profile as the crown changes), plus a
// popover with the follower's status, the crown-churn statistics ("how often does the
// best profile change?"), and the recent crown-change ledger. The popover reads top-down:
// the profile being followed and its cost first, then the rule that picks it, the speed
// limit over that rule, the other verdicts for reference, and the churn of the pooled crown. Tracking (the ledger)
// runs whether or not the switch is on — the switch only arms the firewall write.
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useFastestGaps } from "../hooks/useFastestGaps";
import { Link as RouterLink } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Collapse from "@mui/material/Collapse";
import Divider from "@mui/material/Divider";
import FormControlLabel from "@mui/material/FormControlLabel";
import IconButton from "@mui/material/IconButton";
import InputAdornment from "@mui/material/InputAdornment";
import Link from "@mui/material/Link";
import Popover from "@mui/material/Popover";
import Radio from "@mui/material/Radio";
import RadioGroup from "@mui/material/RadioGroup";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import EmojiEventsIcon from "@mui/icons-material/EmojiEvents";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";

import { api } from "../api/client";
import type { CrownFollowStatus, CrowningPolicy, LegGap, LegGaps } from "../api/types";
import { fmtTimeShort, parseApiDate } from "../utils/format";

const POLL_MS = 60_000;

function fmtHours(h: number | null | undefined): string {
  if (h == null || Number.isNaN(h)) return "—";
  if (h < 1) return `${Math.round(h * 60)} min`;
  if (h < 48) return `${h.toFixed(h < 10 ? 1 : 0)} h`;
  return `${(h / 24).toFixed(1)} d`;
}

function fmtDateShort(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = parseApiDate(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// The rankings Auto-apply can follow, in the order the popover lists them. `hint` is the
// one line under each option; the long form stays in the tooltip.
const POLICIES: { value: CrowningPolicy; name: string; hint: string; tip: string }[] = [
  {
    value: "fused",
    name: "Overall",
    hint: "Pooled results corrected by every duel. The default.",
    tip: "Act on the one ranking fitted over BOTH records — each profile's pooled median, corrected by every head-to-head round it fought. See the Overall page.",
  },
  {
    value: "pooled",
    name: "Pooled overall",
    hint: "Best all-time average.",
    tip: "Act on the all-time pooled Overall argmax — the stable, weather-averaged map of the whole field.",
  },
  {
    value: "duel",
    name: "Duel champion",
    hint: "Latest head-to-head winner.",
    tip: "Act on the duel ladder's latest head-to-head champion (interleaved same-weather verdicts). Falls back to the pooled crown when no decisive fresh duel exists.",
  },
  {
    value: "math",
    name: "Fastest on paper",
    hint: "Lowest FCP × LCP. Ignores smoothness.",
    tip: "The confident profile with the lowest median FCP × median LCP in raw milliseconds. Has no tie test, so it can switch on small differences. Falls back to the pooled crown when no confident profile has both readings.",
  },
  {
    value: "lcp_first",
    name: "LCP first",
    hint: "Best main-content time, then fastest first paint.",
    tip: "Every confident profile whose median LCP is within the allowance of the best LCP qualifies, and the fastest median FCP among them is followed. Raw milliseconds, no weights. Falls back to the pooled crown when no confident profile has both readings.",
  },
  {
    value: "fcp_first",
    name: "FCP first",
    hint: "Best first paint, then fastest main content.",
    tip: "Every confident profile whose median FCP is within the allowance of the best FCP qualifies, and the fastest median LCP among them is followed. Raw milliseconds, no weights. Falls back to the pooled crown when no confident profile has both readings.",
  },
];

const policyName = (p: string | undefined) => POLICIES.find((x) => x.value === p)?.name ?? "pooled overall";

// Same colour rule as FastestGapLine: amber past 0%, red at 25% or more, noise never counts.
function gapTone(g?: LegGap): "success" | "neutral" | "warning" | "error" {
  if (!g) return "neutral";
  if (g.is_fastest) return "success";
  if (!g.clear || g.delta_ms <= 0) return "neutral";
  return g.pct >= 25 ? "error" : "warning";
}

// One leg of "how far behind the fastest is this profile?" as a small tile: the verdict
// large, the percent beneath it.
function LegTile({ leg, gap }: { leg: "LCP" | "FCP"; gap?: LegGap }) {
  const tone = gapTone(gap);
  let value = "—";
  let sub = "no median yet";
  if (gap?.is_fastest) {
    value = "Fastest";
    sub = `${gap.ms.toFixed(0)} ms`;
  } else if (gap && !gap.clear) {
    value = "Within noise";
    sub = `${gap.ms.toFixed(0)} ms`;
  } else if (gap) {
    const slower = gap.delta_ms > 0;
    value = `${slower ? "+" : "−"}${Math.abs(gap.delta_ms).toFixed(0)} ms`;
    sub = `${Math.abs(gap.pct).toFixed(0)}% ${slower ? "slower" : "faster"}`;
  }
  const color =
    tone === "success"
      ? "success.main"
      : tone === "error"
        ? "error.main"
        : tone === "warning"
          ? "warning.main"
          : "text.primary";
  return (
    <Box sx={{ bgcolor: "background.default", borderRadius: 1, p: 1, minWidth: 0 }}>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
        {leg} vs fastest
      </Typography>
      <Typography
        sx={{ fontWeight: 700, fontSize: 16, color, display: "flex", alignItems: "center", gap: 0.5 }}
      >
        {tone === "error" && <WarningAmberIcon sx={{ fontSize: 16 }} />}
        {value}
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ fontVariantNumeric: "tabular-nums" }}>
        {sub}
      </Typography>
    </Box>
  );
}

// "Download: 880Mbit q6300 t3 i60 ecn | Upload: 880Mbit q500 t3 i60 ecn" → two rows. Any
// label that is not in that shape is shown as-is.
function shapingRows(label: string | null | undefined): { dir: string; text: string }[] | null {
  if (!label) return null;
  const rows = label.split("|").map((part) => {
    const m = part.trim().match(/^(Download|Upload):\s*(.*)$/i);
    return m ? { dir: m[1].toLowerCase() === "download" ? "↓ Down" : "↑ Up", text: m[2] } : null;
  });
  return rows.every(Boolean) ? (rows as { dir: string; text: string }[]) : null;
}

// Which legs of a profile clearly exceed the speed budget (gaps inside the noise never count,
// the same rule `crowning.budget_check` applies).
function overBudget(
  gaps: LegGaps | null | undefined,
  budget: { enabled: boolean; lcp_pct: number; fcp_pct: number },
): string[] {
  if (!budget.enabled || !gaps) return [];
  const out: string[] = [];
  if (gaps.lcp?.clear && gaps.lcp.pct > budget.lcp_pct) out.push("LCP");
  if (gaps.fcp?.clear && gaps.fcp.pct > budget.fcp_pct) out.push("FCP");
  return out;
}

function SectionTitle({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
      <Typography
        variant="overline"
        color="text.secondary"
        sx={{ lineHeight: 1.5, letterSpacing: "0.06em", fontWeight: 600 }}
      >
        {children}
      </Typography>
      {action}
    </Stack>
  );
}

export default function FollowBest() {
  const [info, setInfo] = useState<CrownFollowStatus | null>(null);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [busy, setBusy] = useState(false); // toggle or sync in flight
  const [showChanges, setShowChanges] = useState(false);
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
          msg: `${leg.toUpperCase()}-first band set to ${v}% of the best ${leg.toUpperCase()}.`,
          sev: "info",
        });
        load();
      })
      .catch((e) => setSnack({ msg: e instanceof Error ? e.message : "Could not save the band.", sev: "error" }))
      .finally(() => setBusy(false));
  };

  // The speed budget: how much slower than the fastest LCP / FCP a followed crown may be.
  const budget = info?.config.speed_budget ?? { enabled: true, lcp_pct: 20, fcp_pct: 25 };
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
        load();
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
        setSnack({
          msg:
            next === "duel"
              ? "Auto-apply now follows the duel champion (falls back to the pooled best when no fresh verdict exists)."
              : next === "fused"
                ? "Auto-apply now follows the Overall ranking — the pooled record and the duels fitted together (the Best profile page)."
                : next === "math"
                  ? "Auto-apply now follows the fastest profile on paper — lowest median FCP × LCP (falls back to the pooled best when none qualifies)."
                  : next === "lcp_first"
                    ? "Auto-apply now follows LCP first — the fastest FCP among profiles within the band of the best LCP (falls back to the pooled best when none qualifies)."
                  : next === "fcp_first"
                    ? "Auto-apply now follows FCP first — the fastest LCP among profiles within the band of the best FCP (falls back to the pooled best when none qualifies)."
                  : "Auto-apply now follows the pooled Overall — the all-time best by average.",
          sev: "info",
        });
        load();
      })
      .catch((e) =>
        setSnack({ msg: e instanceof Error ? e.message : "Could not change policy.", sev: "error" }),
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
  const legGaps = useFastestGaps([
    followedFp,
    info?.duel_champion?.fingerprint,
    last?.crown_fingerprint,
  ]);
  const pooledDiffers = !!(
    last?.crown_fingerprint &&
    followedFp &&
    last.crown_fingerprint !== followedFp
  );
  const changes = (info?.events ?? []).filter((e) => e.kind === "change").slice(0, 5);

  const followedGaps = followedFp ? legGaps.gaps[followedFp] : null;
  const shaping = shapingRows(followedLabel);
  // When the follower wrote something other than the chosen policy's own pick (a fallback,
  // or the speed budget passing the pick over), say why in one line.
  const fallbackNote =
    followedSource !== policy
      ? last?.governing_detail ||
        (policy === "duel"
          ? "No fresh duel verdict — following the pooled crown instead."
          : policy === "fused"
            ? "The Overall ranking names nobody yet — following the pooled crown instead."
            : policy === "math" || policy === "lcp_first" || policy === "fcp_first"
              ? "No confident FCP and LCP reading yet — following the pooled crown instead."
              : null)
      : null;
  const pctField = (
    label: string,
    value: string,
    onChange: (v: string) => void,
    onCommit: () => void,
    disabled: boolean,
    max: number,
    width?: number,
  ) => (
    <TextField
      size="small"
      sx={width ? { width } : undefined}
      type="number"
      label={label}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onBlur={onCommit}
      onKeyDown={(e) => {
        if (e.key === "Enter") onCommit();
      }}
      disabled={disabled}
      slotProps={{
        input: { endAdornment: <InputAdornment position="end">%</InputAdornment> },
        htmlInput: { min: 0, max, step: 1, inputMode: "numeric", style: { fontSize: 16 } },
      }}
    />
  );
  const duelFp = info?.duel_champion?.fingerprint;
  const duelOver = duelFp ? overBudget(legGaps.gaps[duelFp], budget) : [];
  const pooledFp = last?.crown_fingerprint ?? null;
  const pooledOver = pooledDiffers && pooledFp ? overBudget(legGaps.gaps[pooledFp], budget) : [];
  const gapSummary = (fp: string | null | undefined) => {
    const g = fp ? legGaps.gaps[fp] : null;
    if (!g) return null;
    const one = (name: string, x?: LegGap) =>
      !x
        ? null
        : x.is_fastest
          ? `${name} fastest`
          : !x.clear
            ? `${name} within noise`
            : `${name} ${x.delta_ms > 0 ? "+" : "−"}${Math.abs(x.delta_ms).toFixed(0)} ms (${x.delta_ms > 0 ? "+" : "−"}${Math.abs(x.pct).toFixed(0)}%)`;
    return [one("LCP", g.lcp), one("FCP", g.fcp)].filter(Boolean) as string[];
  };
  const others: { fp: string; name: string; role: string; over: string[]; note?: string }[] = [];
  if (pooledDiffers && pooledFp)
    others.push({
      fp: pooledFp,
      name: last?.crown_name || last?.crown_label || pooledFp,
      role: "Pooled overall winner · its changes are tracked below",
      over: pooledOver,
    });
  if (info?.duel_champion && duelFp !== followedFp)
    others.push({
      fp: info.duel_champion.fingerprint,
      name: info.duel_champion.name || info.duel_champion.label || info.duel_champion.fingerprint,
      role: `Duel champion · duel #${info.duel_champion.duel_id}${info.duel_champion.decisive ? "" : " · by draws only"}`,
      over: duelOver,
    });
  const intervalH = Math.round(((info?.config.interval_minutes ?? 360) / 60) * 10) / 10;

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
          <IconButton size="small" onClick={(e) => setAnchor(e.currentTarget)}>
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
          onClick={(e) => setAnchor(e.currentTarget as HTMLElement)}
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
        slotProps={{ paper: { sx: { p: 2, width: 360, maxWidth: "95vw", borderRadius: 2 } } }}
      >
        <Stack spacing={3}>
          {/* 1 — One switch, named for what it does. Its state is spelled out in the
              status pill under the profile name, not in a second chip up here. */}
          <Stack direction="row" alignItems="center" spacing={1}>
            <Typography sx={{ flexGrow: 1, fontWeight: 650, fontSize: 16, letterSpacing: "-0.01em" }}>
              Auto-apply best profile
            </Typography>
            {/* The popover always carries the switch — on phones it's the only one (the
                toolbar switch is hidden on xs to keep the update button visible). */}
            <Switch
              checked={enabled}
              disabled={busy || info === null}
              onChange={(e) => toggle(e.target.checked)}
              inputProps={{ "aria-label": "Auto-apply best" }}
            />
          </Stack>

          {/* 2 — The answer first: which profile automation follows, whether the firewall
              is on it, and what it costs against the fastest legs. What the follower WRITES
              is the governing crown — the one the crowning policy resolved — never the
              pooled crown, which is shown below as a reference. */}
          <Box sx={{ bgcolor: "action.hover", borderRadius: 1.5, p: 2 }}>
            <Stack spacing={2}>
              <Box>
                <Typography variant="caption" color="text.secondary">
                  {last?.on_crown === true ? "On the firewall now" : "Best profile"} · by{" "}
                  {policyName(followedSource)}
                </Typography>
                {followedFp ? (
                  <Link
                    component={RouterLink}
                    to={`/profiles/${encodeURIComponent(followedFp)}`}
                    onClick={() => setAnchor(null)}
                    sx={{
                      display: "block",
                      fontSize: 22,
                      fontWeight: 700,
                      letterSpacing: "-0.02em",
                      lineHeight: 1.25,
                    }}
                  >
                    {followedName}
                  </Link>
                ) : (
                  <Typography sx={{ fontSize: 18, fontWeight: 600 }} color="text.secondary">
                    {stats?.current_crown_name ||
                      stats?.current_crown_label ||
                      stats?.current_crown_fingerprint ||
                      "No confident crown yet"}
                  </Typography>
                )}
                <Stack direction="row" spacing={1} sx={{ mt: 1 }} flexWrap="wrap" useFlexGap>
                  {!enabled ? (
                    <Chip size="small" label="Tracking only — not applied" variant="outlined" />
                  ) : last?.on_crown === true ? (
                    <Chip size="small" icon={<CheckCircleIcon />} label="Applied" color="success" variant="outlined" />
                  ) : last?.on_crown === false ? (
                    <Chip
                      size="small"
                      icon={<WarningAmberIcon />}
                      label="Firewall is on a different profile"
                      color="warning"
                      variant="outlined"
                    />
                  ) : null}
                </Stack>
                {fallbackNote && (
                  <Typography variant="caption" color="warning.main" sx={{ display: "block", mt: 1 }}>
                    {fallbackNote}
                  </Typography>
                )}
              </Box>

              {followedFp && (
                <Tooltip title="Each leg against the field's fastest median on that leg. A gap counts only when it clears twice the noise of the two medians.">
                  <Box sx={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 1 }}>
                    <LegTile leg="LCP" gap={followedGaps?.lcp} />
                    <LegTile leg="FCP" gap={followedGaps?.fcp} />
                  </Box>
                </Tooltip>
              )}

              {/* The settings that name stands for, one row per direction. */}
              {followedName !== followedLabel && followedLabel && (
                shaping ? (
                  <Box
                    component="dl"
                    sx={{ m: 0, display: "grid", gridTemplateColumns: "auto 1fr", columnGap: 1, rowGap: 0.5 }}
                  >
                    {shaping.map((r) => (
                      <Box key={r.dir} sx={{ display: "contents" }}>
                        <Typography component="dt" variant="caption" color="text.secondary">
                          {r.dir}
                        </Typography>
                        <Typography
                          component="dd"
                          variant="caption"
                          sx={{ m: 0, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace" }}
                        >
                          {r.text}
                        </Typography>
                      </Box>
                    ))}
                  </Box>
                ) : (
                  <Typography variant="caption" color="text.secondary" title={followedLabel}>
                    {followedLabel}
                  </Typography>
                )
              )}

              {(last?.apply_skipped || last?.error) && (
                <Alert severity={last?.error ? "error" : "info"} sx={{ py: 0 }}>
                  {last?.error || last?.apply_skipped}
                </Alert>
              )}

              <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1}>
                <Typography variant="caption" color="text.secondary">
                  Checked {info?.status.last_check_at ? fmtTimeShort(info.status.last_check_at) : "—"}
                  <br />
                  Full check every {intervalH} h
                </Typography>
                <Button
                  variant="outlined"
                  onClick={syncNow}
                  disabled={busy}
                  startIcon={busy ? <CircularProgress size={14} /> : undefined}
                  sx={{ flexShrink: 0, whiteSpace: "nowrap" }}
                >
                  Check now
                </Button>
              </Stack>
            </Stack>
          </Box>

          {/* 3 — The CROWNING POLICY: which verdict the follower acts on. One radio list;
              the LCP-first / FCP-first allowance appears only under its own option, since
              it means nothing to the others. */}
          <Box>
            <SectionTitle>Pick the best profile by</SectionTitle>
            <RadioGroup
              value={policy}
              onChange={(e) => setPolicy(e.target.value as CrowningPolicy)}
              sx={{ gap: 0.5 }}
            >
              {POLICIES.map((p) => {
                const selected = policy === p.value;
                const leg = p.value === "lcp_first" ? "lcp" : p.value === "fcp_first" ? "fcp" : null;
                const other = leg === "lcp" ? "FCP" : "LCP";
                return (
                  <Box
                    key={p.value}
                    sx={{
                      border: 1,
                      borderColor: selected ? "primary.main" : "transparent",
                      bgcolor: selected ? "action.hover" : "transparent",
                      borderRadius: 1,
                      px: 1,
                      py: 0.5,
                    }}
                  >
                    <Tooltip title={p.tip} placement="left" enterDelay={600}>
                      <FormControlLabel
                        value={p.value}
                        disabled={busy}
                        control={<Radio size="small" sx={{ alignSelf: "flex-start", mt: -0.5 }} />}
                        sx={{ m: 0, alignItems: "flex-start", width: "100%" }}
                        label={
                          <Box>
                            <Typography variant="body2" sx={{ fontWeight: 600 }}>
                              {p.name}
                            </Typography>
                            <Typography variant="caption" color="text.secondary">
                              {p.hint}
                            </Typography>
                          </Box>
                        }
                      />
                    </Tooltip>
                    {selected && leg && (
                      <Stack spacing={0.5} sx={{ pl: 4, pt: 1, pb: 1 }}>
                        {pctField(
                          `${leg.toUpperCase()} allowance`,
                          bandDraft[leg],
                          (v) => setBandDraft((d) => ({ ...d, [leg]: v })),
                          () => saveBand(leg),
                          busy,
                          50,
                          150,
                        )}
                        <Typography variant="caption" color="text.secondary">
                          Accept up to this much slower {leg.toUpperCase()} than the best, to get a
                          faster {other}.
                        </Typography>
                      </Stack>
                    )}
                  </Box>
                );
              })}
            </RadioGroup>
          </Box>

          {/* 4 — The speed budget: a guardrail over whichever ranking is chosen. */}
          <Box>
            <SectionTitle
              action={
                <Switch
                  checked={budget.enabled}
                  onChange={(e) => saveBudget({ enabled: e.target.checked })}
                  disabled={busy}
                  inputProps={{ "aria-label": "Speed limit" }}
                />
              }
            >
              Speed limit
            </SectionTitle>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 2 }}>
              {budget.enabled
                ? "Never follow a profile clearly slower than the fastest by more than:"
                : "Off — the chosen ranking is followed however slow its pick is."}
            </Typography>
            <Box
              sx={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: 2,
                opacity: budget.enabled ? 1 : 0.5,
              }}
            >
              {pctField(
                "Max LCP slowdown",
                budgetDraft.lcp,
                (v) => setBudgetDraft((d) => ({ ...d, lcp: v })),
                () => Number(budgetDraft.lcp) !== budget.lcp_pct && saveBudget({ lcp_pct: Number(budgetDraft.lcp) }),
                busy || !budget.enabled,
                200,
              )}
              {pctField(
                "Max FCP slowdown",
                budgetDraft.fcp,
                (v) => setBudgetDraft((d) => ({ ...d, fcp: v })),
                () => Number(budgetDraft.fcp) !== budget.fcp_pct && saveBudget({ fcp_pct: Number(budgetDraft.fcp) }),
                busy || !budget.enabled,
                200,
              )}
            </Box>
            {budget.enabled && (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
                If the pick is over the limit, the first ranking whose pick fits is followed instead.
              </Typography>
            )}
          </Box>

          {/* 5 — The other verdicts, for reference. Never written to the firewall. */}
          {(others.length > 0 || !info?.duel_champion) && (
            <Box>
              <SectionTitle>Other winners · not applied</SectionTitle>
              <Stack divider={<Divider flexItem />} spacing={1}>
                {others.map((o) => {
                  const parts = gapSummary(o.fp);
                  return (
                    <Stack key={o.fp} direction="row" spacing={1} justifyContent="space-between">
                      <Box sx={{ minWidth: 0 }}>
                        <Link
                          component={RouterLink}
                          to={`/profiles/${encodeURIComponent(o.fp)}`}
                          onClick={() => setAnchor(null)}
                          variant="body2"
                          sx={{ fontWeight: 600 }}
                        >
                          {o.name}
                        </Link>
                        <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                          {o.role}
                        </Typography>
                      </Box>
                      <Box sx={{ textAlign: "right", flexShrink: 0 }}>
                        {o.over.length > 0 && (
                          <Typography
                            variant="caption"
                            color="error.main"
                            sx={{ display: "flex", alignItems: "center", gap: 0.5, fontWeight: 600, justifyContent: "flex-end" }}
                          >
                            <WarningAmberIcon sx={{ fontSize: 14 }} />
                            Over speed limit
                          </Typography>
                        )}
                        {parts?.map((t) => (
                          <Typography
                            key={t}
                            variant="caption"
                            color="text.secondary"
                            sx={{ display: "block", fontVariantNumeric: "tabular-nums" }}
                          >
                            {t}
                          </Typography>
                        ))}
                      </Box>
                    </Stack>
                  );
                })}
                {!info?.duel_champion && (
                  <Typography variant="caption" color="text.secondary">
                    No fresh duel verdict yet.
                  </Typography>
                )}
              </Stack>
            </Box>
          )}

          <Divider sx={{ mx: -2 }} />

          {/* 6 — Churn of the POOLED crown: how often does the best profile change? Led by
              one number, the supporting readings beneath it. */}
          <Box>
            <SectionTitle>How often the pooled winner changes</SectionTitle>
            {stats && stats.tracked_since ? (
              <>
                <Stack direction="row" alignItems="baseline" spacing={1}>
                  <Typography sx={{ fontSize: 22, fontWeight: 700, letterSpacing: "-0.02em" }}>
                    {stats.changes_7d} {stats.changes_7d === 1 ? "change" : "changes"}
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    in the last 7 days
                  </Typography>
                </Stack>
                <Box sx={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 1, mt: 1 }}>
                  {[
                    ["24 h", `${stats.changes_24h}`],
                    ["30 days", `${stats.changes_30d}`],
                    ["Per day", stats.changes_per_day != null ? `${stats.changes_per_day}` : "—"],
                    ["Current reign", fmtHours(stats.current_reign_hours)],
                    ["Median reign", fmtHours(stats.median_reign_hours)],
                    ["Tracked since", fmtDateShort(stats.tracked_since)],
                  ].map(([k, v]) => (
                    <Box key={k} sx={{ bgcolor: "action.hover", borderRadius: 1, p: 1 }}>
                      <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                        {k}
                      </Typography>
                      <Typography variant="body2" sx={{ fontWeight: 650, fontVariantNumeric: "tabular-nums" }}>
                        {v}
                      </Typography>
                    </Box>
                  ))}
                </Box>
              </>
            ) : (
              <Typography variant="body2" color="text.secondary">
                No observations yet — this appears after the first check.
              </Typography>
            )}

            {changes.length > 0 && (
              <Box sx={{ mt: 2 }}>
                <Button
                  size="small"
                  onClick={() => setShowChanges((v) => !v)}
                  endIcon={showChanges ? <ExpandLessIcon /> : <ExpandMoreIcon />}
                  sx={{ px: 0, minWidth: 0, color: "text.secondary" }}
                >
                  Recent changes ({changes.length})
                </Button>
                <Collapse in={showChanges}>
                  <Stack spacing={1} sx={{ mt: 1 }}>
                    {changes.map((e) => (
                      <Box
                        key={e.id}
                        sx={{ display: "grid", gridTemplateColumns: "104px 1fr", gap: 1, alignItems: "baseline" }}
                      >
                        <Typography variant="caption" color="text.secondary">
                          {fmtTimeShort(e.created_at)}
                        </Typography>
                        <Typography variant="caption">
                          {e.previous_fingerprint
                            ? `${e.previous_name || e.previous_label || e.previous_fingerprint} → ${
                                e.name || e.label || e.fingerprint
                              }`
                            : `Tracking started (${e.name || e.label || e.fingerprint})`}
                          {e.applied && " · applied"}
                          {e.error && (
                            <Box component="span" sx={{ color: "error.main" }}>
                              {" "}
                              · apply failed
                            </Box>
                          )}
                        </Typography>
                      </Box>
                    ))}
                  </Stack>
                </Collapse>
              </Box>
            )}
          </Box>
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

import { useMemo, useState } from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Divider from "@mui/material/Divider";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import CasinoIcon from "@mui/icons-material/Casino";

import type { ExploreBet, ExploreCalibration } from "../api/types";

// Iteration lengths worth offering. 5 is one runner chunk — the cheap "did this go
// anywhere?" reading; the top-up is the long "settle it" answer and is rarely what a batch
// wants, so it sits last rather than first.
const LENGTHS = [3, 5, 10, 15];

function fmt(n: number | null | undefined, digits = 1): string {
  return n == null ? "—" : n.toFixed(digits);
}

function confidenceColor(c: string | null | undefined) {
  return c === "high" ? "success" : c === "medium" ? "warning" : "default";
}

/**
 * "Run the smartest bets": queue the top N recommendations at M iterations each.
 *
 * Explore routinely produces ten proposals worth measuring and, until this, exactly one
 * button per row to measure them with. The batch is the missing verb — but *which* N is the
 * substance, not the count. The page's own list is ranked by an upper confidence bound,
 * which is right for exploring (uncertainty is an attraction when you're deciding where to
 * look) and wrong for deciding what to spend a night running. So the default order here is
 * the pessimistic one: each candidate scored at the low end of a band widened by what its
 * evidence class has *actually* missed by, measured from the recommendation ledger.
 *
 * The preview is the honest part. It shows the floor beside the prediction, says which band
 * was used, and marks whether that floor still clears the best profile measured — so
 * queueing five is a decision rather than a leap of faith.
 */
export default function BestBetsDialog({
  open,
  bets,
  calibration,
  bestOverall,
  busy,
  onClose,
  onRun,
}: {
  open: boolean;
  bets: ExploreBet[];
  calibration: Record<string, ExploreCalibration> | undefined;
  bestOverall: number | null;
  busy: boolean;
  onClose: () => void;
  onRun: (count: number, iterations: number, rank: "confidence" | "upside") => void;
}) {
  const [count, setCount] = useState(3);
  const [iterations, setIterations] = useState(5);
  const [rank, setRank] = useState<"confidence" | "upside">("confidence");

  const ordered = useMemo(() => {
    if (rank === "confidence") return bets;
    // The page's exploring order, so the preview matches what the server will pick.
    return [...bets].sort((a, b) => (b.upside ?? 0) - (a.upside ?? 0));
  }, [bets, rank]);

  const picked = ordered.slice(0, Math.min(count, ordered.length));
  const clearing = picked.filter((b) => b.clears_bar).length;

  // Is any of this backed by a track record yet, or is it the model's own opinion?
  const graded = useMemo(
    () => Object.values(calibration ?? {}).reduce((n, c) => n + (c.trusted ? c.graded : 0), 0),
    [calibration],
  );

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <CasinoIcon color="primary" /> Run the best bets
      </DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Queue the top recommendations back to back. Each one applies its profile,
          benchmarks, and restores your settings when its turn comes — they line up behind
          whatever is running now, so you can start them all and walk away.
        </Typography>

        <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ mb: 2 }}>
          <TextField
            select
            size="small"
            label="How many"
            value={count}
            onChange={(e) => setCount(Number(e.target.value))}
            sx={{ minWidth: 120 }}
          >
            {Array.from({ length: Math.min(12, Math.max(1, bets.length)) }, (_, i) => i + 1).map(
              (n) => (
                <MenuItem key={n} value={n}>
                  Top {n}
                </MenuItem>
              ),
            )}
          </TextField>
          <TextField
            select
            size="small"
            label="Iterations each"
            value={iterations}
            onChange={(e) => setIterations(Number(e.target.value))}
            sx={{ minWidth: 150 }}
          >
            {LENGTHS.map((n) => (
              <MenuItem key={n} value={n}>
                {n} iterations
              </MenuItem>
            ))}
          </TextField>
          <TextField
            select
            size="small"
            label="Pick by"
            value={rank}
            onChange={(e) => setRank(e.target.value as "confidence" | "upside")}
            sx={{ minWidth: 220 }}
            helperText={
              rank === "confidence"
                ? "Best bets — good even if the model is wrong"
                : "Biggest upside — drawn to what we know least"
            }
          >
            <MenuItem value="confidence">Confidence (what to back)</MenuItem>
            <MenuItem value="upside">Upside (what to explore)</MenuItem>
          </TextField>
        </Stack>

        <Divider sx={{ mb: 1.5 }} />

        <Typography variant="subtitle2" gutterBottom>
          These {picked.length} would be queued
          {bestOverall != null && (
            <Typography component="span" variant="caption" color="text.secondary">
              {" "}
              · best measured is {fmt(bestOverall)}
              {clearing > 0 ? ` · ${clearing} clear it even at their floor` : ""}
            </Typography>
          )}
        </Typography>

        <Stack spacing={1} sx={{ mb: 2 }}>
          {picked.map((bet, i) => (
            <Box
              key={bet.changes.map((c) => c.key).join("|") + i}
              sx={{
                p: 1,
                border: 1,
                borderColor: "divider",
                borderRadius: 1,
                display: "flex",
                flexWrap: "wrap",
                gap: 1,
                alignItems: "center",
              }}
            >
              <Typography variant="body2" sx={{ flexGrow: 1, minWidth: 180 }}>
                <strong>{bet.parent.name || bet.parent.label}</strong>
                {bet.changes.map((c) => (
                  <span key={c.key}>
                    {" · "}
                    {c.pipe} {c.field_label} {c.from} → <strong>{c.to}</strong>
                  </span>
                ))}
              </Typography>
              <Tooltip
                title={`Predicted ${fmt(bet.predicted)} ± ${fmt(bet.confidence_band)} (${bet.calibration_basis}). The floor is what it is worth if the model is wrong by its usual amount.`}
              >
                <Typography variant="caption" color="text.secondary">
                  {fmt(bet.predicted)} → floor {fmt(bet.confidence_score)}
                </Typography>
              </Tooltip>
              <Chip
                size="small"
                label={bet.confidence}
                color={confidenceColor(bet.confidence) as "success" | "warning" | "default"}
                variant="outlined"
              />
              {bet.clears_bar && (
                <Chip size="small" label="beats best" color="success" variant="filled" />
              )}
            </Box>
          ))}
        </Stack>

        {graded > 0 ? (
          <Alert severity="success">
            Ranked against a real track record: {graded} past recommendation
            {graded === 1 ? "" : "s"} have been graded, so each band is widened by what that
            kind of evidence has actually missed by on your link.
          </Alert>
        ) : (
          <Alert severity="info">
            No graded recommendations yet, so these bands are the model's own estimate rather
            than a measured track record. Run a few and the ranking starts correcting itself —
            that is what the "Was the data right?" card below is for.
          </Alert>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <Button
          variant="contained"
          startIcon={<CasinoIcon />}
          disabled={busy || picked.length === 0}
          onClick={() => onRun(count, iterations, rank)}
        >
          {busy ? "Queueing…" : `Queue ${picked.length} test${picked.length === 1 ? "" : "s"}`}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

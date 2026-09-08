import { useMemo } from "react";
import Chip from "@mui/material/Chip";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import ScienceIcon from "@mui/icons-material/Science";

import type { LeverLedger, LeverRow } from "../api/types";
import { FoldCard, HelpTip } from "./Explain";
import { fmtNum } from "../utils/format";

/**
 * The lever ledger: every duel match between two profiles that differ in exactly one
 * setting, pooled per lever as the effect of moving it UP, beside what fq_codel theory
 * predicts on an unsaturated link. Lives on the Levers page; read-only.
 */
const DIRECTION_WORD: Record<LeverRow["direction"], string> = {
  higher: "higher helps",
  lower: "lower helps",
  none: "no effect",
  thin: "too thin",
};
const PREDICTION_WORD: Record<LeverRow["prediction"], string> = {
  null: "no effect",
  interior: "optimum inside the range",
  conditional: "depends",
  unknown: "no prediction",
};
const AGREEMENT_CHIP: Record<
  LeverRow["agreement"],
  { label: string; color: "success" | "warning" | "info" | "default" }
> = {
  as_predicted: { label: "as predicted", color: "success" },
  consistent: { label: "consistent", color: "success" },
  surprise: { label: "surprise", color: "warning" },
  flat_here: { label: "flat here", color: "info" },
  measured: { label: "measured", color: "info" },
  untested: { label: "untested", color: "default" },
};

export default function LeverLedgerCard({ book }: { book: LeverLedger | null }) {
  const rows = book?.levers ?? [];
  const fought = rows.filter((r) => r.rounds > 0);
  const surprises = rows.filter((r) => r.agreement === "surprise").length;
  const crownKeys = useMemo(
    () => Array.from(new Set(rows.flatMap((r) => Object.keys(r.crown_margin_up ?? {})))),
    [rows],
  );
  return (
    <FoldCard
      icon={<ScienceIcon color="primary" />}
      title="What one lever does, measured in the ring"
      defaultOpen={surprises > 0}
      summary={
        book ? (
          <>
            {fought.length} lever{fought.length === 1 ? "" : "s"} with paired duel evidence from{" "}
            {book.matches_used} single-lever match{book.matches_used === 1 ? "" : "es"}
            {surprises > 0 ? ` · ${surprises} contradict${surprises === 1 ? "s" : ""} the mechanism` : ""}.
            <HelpTip title="Every duel match between two profiles that differ in exactly one setting is a paired, interleaved, same-weather reading of that setting — the controlled version of the matched pairs above. Margins are the effect of moving the lever UP (higher value minus lower), in Overall points. Beside each lever: what fq_codel theory predicts on an unsaturated link, and whether the ring agrees. A prediction that fails is the interesting row." />
          </>
        ) : (
          "Reading the duel ledger…"
        )
      }
    >
      {!book ? (
        <Typography variant="body2" color="text.secondary">
          The ledger has not loaded.
        </Typography>
      ) : (
        <>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
            {book.sessions_analyzed} session{book.sessions_analyzed === 1 ? "" : "s"} read · {book.matches_used} used ·{" "}
            {book.matches_skipped} skipped (more than one lever apart, or no settings on record) · {book.matches_aborted} aborted.
            A direction is stated only past {book.min_rounds} rounds and p &lt; {book.alpha}. A lever session
            (above) fills this table on purpose; ordinary duels add to it whenever two profiles happen to be
            one setting apart.
          </Typography>
          <TableContainer sx={{ overflowX: "auto" }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Lever</TableCell>
                  <TableCell>Predicted (unsaturated link)</TableCell>
                  <TableCell>Measured</TableCell>
                  <TableCell align="right">Rounds ↑–↓</TableCell>
                  <TableCell align="right">Δ moving up</TableCell>
                  {crownKeys.map((k) => (
                    <TableCell key={k} align="right">
                      Δ {k}
                    </TableCell>
                  ))}
                  <TableCell>Verdict</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {rows.map((r) => {
                  const chip = AGREEMENT_CHIP[r.agreement] ?? AGREEMENT_CHIP.untested;
                  const p = r.paired_p ?? r.sign_p;
                  return (
                    <TableRow key={`${r.pipe}:${r.field}`} hover sx={{ opacity: r.rounds > 0 ? 1 : 0.6 }}>
                      <TableCell sx={{ whiteSpace: "nowrap" }}>
                        {r.pipe} {r.field_label}
                        {r.transitions.length > 0 && (
                          <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                            {r.transitions
                              .slice(0, 3)
                              .map((t) => `${t.from_shown}→${t.to_shown}: ${t.rounds} rds, ${t.median_margin_up == null ? "—" : (t.median_margin_up > 0 ? "+" : "") + fmtNum(t.median_margin_up, 1)}`)
                              .join(" · ")}
                          </Typography>
                        )}
                      </TableCell>
                      <TableCell>
                        <Tooltip title={`${r.mechanism} ${r.unsaturated}`}>
                          <Typography variant="body2" sx={{ cursor: "help" }}>
                            {PREDICTION_WORD[r.prediction] ?? r.prediction}
                          </Typography>
                        </Tooltip>
                      </TableCell>
                      <TableCell sx={{ whiteSpace: "nowrap" }}>
                        {DIRECTION_WORD[r.direction] ?? r.direction}
                        {p != null && r.rounds > 0 && (
                          <Typography component="span" variant="caption" color="text.secondary">
                            {" "}
                            p={fmtNum(p, 3)}
                          </Typography>
                        )}
                      </TableCell>
                      <TableCell align="right" sx={{ whiteSpace: "nowrap" }}>
                        {r.rounds > 0 ? `${r.wins_higher}–${r.wins_lower}` : "—"}
                      </TableCell>
                      <TableCell
                        align="right"
                        sx={{
                          whiteSpace: "nowrap",
                          fontWeight: 600,
                          color:
                            r.median_margin_up == null
                              ? "text.secondary"
                              : r.median_margin_up > 0
                                ? "success.main"
                                : r.median_margin_up < 0
                                  ? "error.main"
                                  : "text.secondary",
                        }}
                      >
                        {r.median_margin_up == null
                          ? "—"
                          : `${r.median_margin_up > 0 ? "+" : ""}${fmtNum(r.median_margin_up, 2)}`}
                      </TableCell>
                      {crownKeys.map((k) => {
                        const v = r.crown_margin_up?.[k];
                        return (
                          <TableCell key={k} align="right" sx={{ whiteSpace: "nowrap" }}>
                            {v == null ? "—" : `${v > 0 ? "+" : ""}${fmtNum(v, 1)}`}
                          </TableCell>
                        );
                      })}
                      <TableCell>
                        <Tooltip title={r.agreement_why}>
                          <Chip size="small" variant="outlined" color={chip.color} label={chip.label} sx={{ height: 20 }} />
                        </Tooltip>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableContainer>
        </>
      )}
    </FoldCard>
  );
}

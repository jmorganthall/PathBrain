import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogContentText from "@mui/material/DialogContentText";
import DialogTitle from "@mui/material/DialogTitle";
import FormControlLabel from "@mui/material/FormControlLabel";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";
import PublishIcon from "@mui/icons-material/Publish";

import type { ApplyProfileChange } from "../api/types";

// The previewed write plan for one apply, awaiting the user's go-ahead. Shared by the
// Settings-Impact "Apply this profile" and the AI page's "Apply suggestion" — one dialog so the
// confirm-before-write experience (exact from→to diff + optional post-apply benchmark) is identical.
export interface ApplyConfirm {
  // A stored-profile fingerprint or, for arbitrary (AI) settings, the target fingerprint.
  fingerprint: string;
  // The raw settings to apply, when this isn't a stored profile (AI suggestion). When set, the
  // caller commits via apply-settings; otherwise via apply-profile(fingerprint).
  settings?: unknown;
  label: string;
  changes: ApplyProfileChange[];
  warnings: string[];
  alreadyApplied: boolean;
}

export default function ApplyConfirmDialog({
  confirm,
  applying,
  runBenchmark,
  onRunBenchmarkChange,
  onCancel,
  onConfirm,
  title = "Apply to firewall",
}: {
  confirm: ApplyConfirm | null;
  applying: boolean;
  runBenchmark: boolean;
  onRunBenchmarkChange: (v: boolean) => void;
  onCancel: () => void;
  onConfirm: () => void;
  title?: string;
}) {
  return (
    <Dialog open={confirm != null} onClose={() => !applying && onCancel()} maxWidth="sm" fullWidth>
      <DialogTitle>{title}</DialogTitle>
      <DialogContent>
        {confirm && (
          <>
            <DialogContentText sx={{ mb: 1 }}>
              Write <b>{confirm.label}</b> to the firewall via the traffic shaper. This changes your
              live network shaping immediately and isn't auto-undone — to revert, apply a different
              profile.
            </DialogContentText>
            {confirm.alreadyApplied ? (
              <Alert severity="info" sx={{ mb: 1 }}>
                The firewall already matches these settings — there's nothing to write.
              </Alert>
            ) : (
              <TableContainer>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>Pipe</TableCell>
                      <TableCell>Field</TableCell>
                      <TableCell align="right">From</TableCell>
                      <TableCell align="right">To</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {confirm.changes.map((c, i) => (
                      <TableRow key={`${c.pipe_uuid}-${c.field}-${i}`}>
                        <TableCell>{c.label}</TableCell>
                        <TableCell>{c.field_label}</TableCell>
                        <TableCell align="right">
                          <Typography component="span" variant="body2" color="text.secondary">
                            {String(c.from ?? "—")}
                          </Typography>
                        </TableCell>
                        <TableCell align="right" sx={{ fontWeight: 700 }}>
                          {String(c.to ?? "—")}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
            )}
            {confirm.warnings.length > 0 && (
              <Alert severity="warning" sx={{ mt: 1 }}>
                {confirm.warnings.map((w, i) => (
                  <div key={i}>{w}</div>
                ))}
              </Alert>
            )}
          </>
        )}
        <FormControlLabel
          sx={{ mt: 1 }}
          control={
            <Checkbox
              checked={runBenchmark}
              onChange={(e) => onRunBenchmarkChange(e.target.checked)}
              disabled={applying}
            />
          }
          label="Run a benchmark after applying (1 iteration)"
        />
      </DialogContent>
      <DialogActions>
        <Button onClick={onCancel} disabled={applying}>
          Cancel
        </Button>
        <Button
          variant="contained"
          color="warning"
          startIcon={applying ? <CircularProgress size={16} color="inherit" /> : <PublishIcon />}
          onClick={onConfirm}
          disabled={applying || (confirm?.alreadyApplied ?? false)}
        >
          {applying ? "Applying…" : "Apply to firewall"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

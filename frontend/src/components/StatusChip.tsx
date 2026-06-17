import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import { fmtDuration } from "../utils/format";

type ChipColor = "default" | "success" | "warning" | "error" | "info";

const MAP: Record<string, ChipColor> = {
  complete: "success",
  completed: "success",
  success: "success",
  running: "info",
  pending: "warning",
  queued: "warning",
  failed: "error",
  error: "error",
};

// When a run is in progress, prefer showing an ETA over the bare word "running".
export default function StatusChip({
  status,
  etaMs,
}: {
  status: string;
  etaMs?: number | null;
}) {
  const key = (status || "").toLowerCase();
  const color = MAP[key] ?? "default";
  const isRunning = key === "running" || key === "pending" || key === "queued";

  let label = status || "unknown";
  if (isRunning && etaMs != null) {
    label = etaMs > 1000 ? `ETA: ${fmtDuration(etaMs)}` : "wrapping up…";
  }

  return (
    <Chip
      size="small"
      color={color}
      variant={color === "default" ? "outlined" : "filled"}
      label={label}
      icon={
        isRunning ? (
          <CircularProgress size={12} sx={{ color: "inherit", ml: 1 }} />
        ) : undefined
      }
    />
  );
}

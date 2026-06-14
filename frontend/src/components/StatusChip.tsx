import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";

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

export default function StatusChip({ status }: { status: string }) {
  const key = (status || "").toLowerCase();
  const color = MAP[key] ?? "default";
  const isRunning = key === "running" || key === "pending" || key === "queued";
  return (
    <Chip
      size="small"
      color={color}
      variant={color === "default" ? "outlined" : "filled"}
      label={status || "unknown"}
      icon={
        isRunning ? (
          <CircularProgress size={12} sx={{ color: "inherit", ml: 1 }} />
        ) : undefined
      }
    />
  );
}

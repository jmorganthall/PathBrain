import Box from "@mui/material/Box";
import LinearProgress from "@mui/material/LinearProgress";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import type { ScoreOut } from "../api/types";
import { sopsColor } from "../theme";
import { fmtScore } from "../utils/format";

export default function SubscoreBreakdown({ score }: { score: ScoreOut }) {
  const keys = Object.keys(score.subscores).sort(
    (a, b) => (score.weights_used[b] ?? 0) - (score.weights_used[a] ?? 0)
  );

  if (keys.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        No subscores available.
      </Typography>
    );
  }

  return (
    <Stack spacing={1.5}>
      {keys.map((k) => {
        const value = score.subscores[k];
        const weight = score.weights_used[k];
        return (
          <Box key={k}>
            <Box sx={{ display: "flex", justifyContent: "space-between", mb: 0.5 }}>
              <Typography variant="body2" sx={{ textTransform: "uppercase", letterSpacing: 0.5 }}>
                {k}
                {weight != null && (
                  <Typography component="span" variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                    weight {fmtScore(weight)}
                  </Typography>
                )}
              </Typography>
              <Typography variant="body2" sx={{ fontWeight: 600, color: sopsColor(value) }}>
                {fmtScore(value)}
              </Typography>
            </Box>
            <LinearProgress
              variant="determinate"
              value={Math.max(0, Math.min(100, value))}
              sx={{
                height: 8,
                borderRadius: 4,
                bgcolor: "rgba(255,255,255,0.06)",
                "& .MuiLinearProgress-bar": { backgroundColor: sopsColor(value) },
              }}
            />
          </Box>
        );
      })}
    </Stack>
  );
}

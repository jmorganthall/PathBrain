import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { sopsColor } from "../theme";

interface Props {
  value: number | null | undefined;
  size?: number;
  label?: string;
}

// Circular score gauge rendered with a conic gradient (Overall/axis scores).
export default function ScoreGauge({ value, size = 200, label = "Overall" }: Props) {
  const v = value == null ? null : Math.max(0, Math.min(100, value));
  const color = sopsColor(v);
  const pct = v ?? 0;
  const ring = `conic-gradient(${color} ${pct * 3.6}deg, rgba(255,255,255,0.08) 0deg)`;
  const inner = size * 0.78;

  return (
    <Box
      sx={{
        width: size,
        height: size,
        borderRadius: "50%",
        background: ring,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Box
        sx={{
          width: inner,
          height: inner,
          borderRadius: "50%",
          bgcolor: "background.paper",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <Typography sx={{ fontSize: size * 0.32, fontWeight: 700, color, lineHeight: 1 }}>
          {v == null ? "—" : Math.round(v)}
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5, px: 1, textAlign: "center" }}>
          {label}
        </Typography>
      </Box>
    </Box>
  );
}

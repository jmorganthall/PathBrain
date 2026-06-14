import Box from "@mui/material/Box";
import CircularProgress from "@mui/material/CircularProgress";
import Typography from "@mui/material/Typography";

export default function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <Box sx={{ display: "flex", flexDirection: "column", alignItems: "center", py: 6, gap: 2 }}>
      <CircularProgress />
      <Typography variant="body2" color="text.secondary">
        {label}
      </Typography>
    </Box>
  );
}

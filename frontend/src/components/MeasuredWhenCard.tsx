import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { api } from "../api/client";
import type { ProfileConditions } from "../api/types";

// When this profile was measured, and whether the machine was slow while it was. The
// machine runs slower for hours every night; a profile measured mostly then wears that in
// its standing. The server counts hours in UTC — shifted here to the viewer's clock.

export default function MeasuredWhenCard({ fingerprint }: { fingerprint: string }) {
  const [data, setData] = useState<ProfileConditions | null>(null);
  useEffect(() => {
    let live = true;
    api
      .profileConditions(fingerprint)
      .then((d) => live && setData(d))
      .catch(() => live && setData(null));
    return () => {
      live = false;
    };
  }, [fingerprint]);
  if (!data || data.runs === 0) return null;
  const offset = -new Date().getTimezoneOffset() / 60; // hours east of UTC
  const local = Array.from({ length: 24 }, (_, h) => data.hours_utc[(((h - offset) % 24) + 24) % 24] ?? 0);
  const max = Math.max(1, ...local);
  return (
    <Card sx={{ mb: 2 }}>
      <CardContent>
        <Typography variant="h6" gutterBottom>
          When it was measured
        </Typography>
        {data.sentence && (
          <Alert severity={data.flag ? "warning" : "info"} variant="outlined" sx={{ mb: 2 }}>
            {data.sentence}
          </Alert>
        )}
        <Stack direction="row" spacing={0.5} alignItems="flex-end" sx={{ height: 48 }}>
          {local.map((n, h) => (
            <Tooltip key={h} title={`${String(h).padStart(2, "0")}:00 — ${n} run${n === 1 ? "" : "s"}`}>
              <Box
                sx={{
                  flex: 1,
                  minWidth: 0,
                  height: `${Math.max(2, (n / max) * 100)}%`,
                  bgcolor: n ? "primary.main" : "divider",
                  borderRadius: 0.5,
                }}
              />
            </Tooltip>
          ))}
        </Stack>
        <Stack direction="row" justifyContent="space-between" sx={{ mt: 0.5 }}>
          {[0, 6, 12, 18, 23].map((h) => (
            <Typography key={h} variant="caption" color="text.secondary">
              {String(h).padStart(2, "0")}:00
            </Typography>
          ))}
        </Stack>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
          {data.runs} run{data.runs === 1 ? "" : "s"} by hour of day (your time)
          {data.runs_with_reading < data.runs
            ? ` · ${data.runs_with_reading} carry a host-speed reading`
            : ""}
        </Typography>
      </CardContent>
    </Card>
  );
}

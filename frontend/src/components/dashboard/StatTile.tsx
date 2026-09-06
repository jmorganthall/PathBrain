// A KPI stat tile: one label, one big value, one line of context. The building block of
// the Dashboard's status strip. The value is the chart — no bar, no gauge — so a row of
// these reads like a NOC wall: state first, explanation on hover.
//
// `tone` drives a small status dot beside the label (never the value's colour: values
// wear text ink so a row of tiles doesn't turn into a traffic light). A tone always
// ships with a caption that says the state in words, so colour is never the only signal.
import type { ReactNode } from "react";
import { Link as RouterLink } from "react-router-dom";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardActionArea from "@mui/material/CardActionArea";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

export type Tone = "good" | "warn" | "bad" | "info" | "idle";

const TONE_COLOR: Record<Tone, string> = {
  good: "#66bb6a",
  warn: "#ffb74d",
  bad: "#ef5350",
  info: "#4dd0e1",
  idle: "#546e7a",
};

export function toneColor(tone: Tone): string {
  return TONE_COLOR[tone];
}

interface Props {
  label: string;
  value: ReactNode;
  // Small unit or qualifier rendered after the value at a lighter weight ("ms", "/day").
  unit?: string;
  caption?: ReactNode;
  tone?: Tone;
  // Pulse the status dot — for "something is happening right now".
  live?: boolean;
  // Explanation shown on hover over the label.
  help?: ReactNode;
  // Something drawn to the right of the value (a sparkline, an icon).
  aside?: ReactNode;
  // Navigate on click.
  to?: string;
  minHeight?: number;
}

export default function StatTile({
  label,
  value,
  unit,
  caption,
  tone,
  live,
  help,
  aside,
  to,
  minHeight = 108,
}: Props) {
  const body = (
    <Box sx={{ p: 1.75, minHeight, display: "flex", flexDirection: "column", gap: 0.5, minWidth: 0 }}>
      <Stack direction="row" spacing={0.75} alignItems="center" sx={{ minWidth: 0 }}>
        {tone && (
          <Box
            component="span"
            aria-hidden="true"
            sx={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              bgcolor: TONE_COLOR[tone],
              flexShrink: 0,
              boxShadow: live ? `0 0 0 0 ${TONE_COLOR[tone]}` : "none",
              animation: live ? "pb-pulse 1.6s ease-out infinite" : "none",
              "@keyframes pb-pulse": {
                "0%": { boxShadow: `0 0 0 0 ${TONE_COLOR[tone]}99` },
                "100%": { boxShadow: `0 0 0 8px ${TONE_COLOR[tone]}00` },
              },
            }}
          />
        )}
        <Tooltip title={help ?? ""} disableHoverListener={!help} arrow enterTouchDelay={0}>
          <Typography
            variant="overline"
            color="text.secondary"
            noWrap
            sx={{ lineHeight: 1.4, letterSpacing: 0.8, cursor: help ? "help" : "default" }}
          >
            {label}
          </Typography>
        </Tooltip>
      </Stack>
      <Stack direction="row" spacing={1} alignItems="flex-end" justifyContent="space-between">
        <Typography
          component="div"
          sx={{
            fontSize: { xs: 26, sm: 30 },
            fontWeight: 600,
            lineHeight: 1.05,
            letterSpacing: -0.5,
            // The value never breaks: "44s" split as "44 / s" reads as two numbers. The
            // aside (a sparkline) gives way instead.
            whiteSpace: "nowrap",
            flexShrink: 0,
          }}
        >
          {value}
          {unit && (
            <Typography component="span" sx={{ fontSize: 14, fontWeight: 500, ml: 0.5 }} color="text.secondary">
              {unit}
            </Typography>
          )}
        </Typography>
        {aside && (
          <Box sx={{ minWidth: 0, display: "flex", alignItems: "flex-end", justifyContent: "flex-end", overflow: "hidden" }}>
            {aside}
          </Box>
        )}
      </Stack>
      {caption != null && (
        <Typography
          variant="caption"
          color="text.secondary"
          component="div"
          sx={{
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
            overflow: "hidden",
            lineHeight: 1.35,
          }}
        >
          {caption}
        </Typography>
      )}
    </Box>
  );

  return (
    <Card sx={{ height: "100%" }}>
      {to ? (
        <CardActionArea component={RouterLink} to={to} sx={{ height: "100%", alignItems: "stretch" }}>
          {body}
        </CardActionArea>
      ) : (
        body
      )}
    </Card>
  );
}

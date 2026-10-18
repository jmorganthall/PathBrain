// The one page header.
//
// Every page used to build its own title row — h4 on most, h5 on some, an icon on a few,
// bold on others, the lead sentence as a caption here and a paragraph there, the actions
// sometimes beside the title and sometimes under it. Twenty-two pages, nine patterns, and
// a reader learns the layout of each page separately. This is the pattern:
//
//   Title ········································ [primary action] [secondary]
//   One plain sentence saying what the page answers.  More ▾
//
// `lead` is the sentence a first-time visitor reads; `more` is everything that used to be
// the opening paragraph, folded behind a "More" link (via Blurb). `actions` is the page's
// Run/Refresh buttons: on a phone they wrap under the title, one per line.
import type { ReactNode } from "react";
import Box from "@mui/material/Box";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import { Blurb } from "./Explain";

export default function PageHeader({
  title,
  icon,
  lead,
  more,
  actions,
  below,
  compact = false,
}: {
  title: ReactNode;
  icon?: ReactNode;
  /** One sentence: what this page answers. */
  lead?: ReactNode;
  /** The longer rationale, folded behind "More". */
  more?: ReactNode;
  /** Buttons/controls for the whole page. */
  actions?: ReactNode;
  /** Anything that must sit directly under the lead (a status chip row, a filter). */
  below?: ReactNode;
  /** Tighter bottom margin for pages whose first card is itself a header. */
  compact?: boolean;
}) {
  return (
    <Box sx={{ mb: compact ? 1.5 : 2.5 }}>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        spacing={1}
        useFlexGap
      >
        <Stack direction="row" spacing={1.25} alignItems="center" sx={{ minWidth: 0 }}>
          {icon && (
            <Box sx={{ display: "flex", color: "primary.main", "& svg": { fontSize: 28 } }} aria-hidden>
              {icon}
            </Box>
          )}
          <Typography variant="h4" component="h1" sx={{ fontWeight: 700, lineHeight: 1.15, wordBreak: "break-word" }}>
            {title}
          </Typography>
        </Stack>
        {actions && (
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap sx={{ flexShrink: 0 }}>
            {actions}
          </Stack>
        )}
      </Stack>
      {lead && (
        <Blurb variant="body2" more={more} sx={{ mt: 0.5, mb: 0, maxWidth: 880 }}>
          {lead}
        </Blurb>
      )}
      {below && <Box sx={{ mt: 1 }}>{below}</Box>}
    </Box>
  );
}

/** A small overline that names the next group of cards on a long page. */
export function SectionLabel({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mt: 1, mb: 1 }}>
      <Typography variant="overline" color="text.secondary" sx={{ letterSpacing: 1, lineHeight: 1.6 }}>
        {children}
      </Typography>
      {right}
    </Stack>
  );
}

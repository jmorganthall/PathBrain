import { alpha, createTheme } from "@mui/material/styles";

// Dark, Material-design theme for PathBrain.
//
// Built to the UI rules in the "ui-dos-and-donts" skill; the rule IDs are noted where a
// value would otherwise look arbitrary. Every colour pair below was checked for contrast
// (WCAG ratio, sRGB luminance):
//
//   text on    default  paper  raised
//   primary     15.0    13.7   12.0
//   secondary    8.5     7.7    6.8
//   disabled     5.9     5.4    4.7   (DK5: still ≥ 4.5 — it is used as caption text)
//   accent      10.1     9.2    8.1
//   error        7.6     6.9    6.1
//
//   dark text on the accent fill: 10.6 (white would be 1.8 — CO4)

/** Surfaces, lowest to highest. DK1: dark gray, never #000. DK4: higher = lighter. */
export const SURFACE = {
  default: "#12161d",
  paper: "#1a1f28",
  raised: "#232a35",
  overlay: "#2b3340",
} as const;

/** Text tones. DK2: off-white primary, softer gray secondary. */
export const TEXT = {
  primary: "#e8eaed",
  secondary: "#aab2bd",
  disabled: "#8b94a1",
  onFill: "#0d1117",
} as const;

/**
 * The palette's named colours, desaturated for a dark background (DK3). Charts and
 * hand-coloured marks read these rather than typing a hex, so a light-mode saturated
 * colour can't drift back in.
 */
export const COLORS = {
  accent: "#6fd0dc",
  violet: "#a88bff",
  purple: "#c58af9",
  pink: "#f48fb1",
  magenta: "#ee7fbf",
  green: "#81c995",
  lightGreen: "#9fd5a6",
  amber: "#f6c06b",
  orange: "#f5a65b",
  red: "#f28b82",
  blue: "#8ab4f8",
  indigo: "#9fa8da",
  slate: "#9aa6b2",
  slateDark: "#6b7884",
  ink: "#e8eaed",
} as const;

// Touch devices only: a 44×44 minimum tap target (MO1). Applied by pointer type rather
// than screen width so a desk mouse keeps the dense tables and a phone keeps its thumb.
const TOUCH = "@media (pointer: coarse)";

export const theme = createTheme({
  palette: {
    mode: "dark",
    primary: { main: COLORS.accent, contrastText: TEXT.onFill },
    secondary: { main: COLORS.violet, contrastText: TEXT.onFill },
    success: { main: COLORS.green, contrastText: TEXT.onFill },
    warning: { main: COLORS.amber, contrastText: TEXT.onFill },
    error: { main: COLORS.red, contrastText: TEXT.onFill },
    info: { main: COLORS.blue, contrastText: TEXT.onFill },
    background: {
      default: SURFACE.default,
      paper: SURFACE.paper,
    },
    text: {
      primary: TEXT.primary,
      secondary: TEXT.secondary,
      disabled: TEXT.disabled,
    },
    divider: "rgba(255,255,255,0.10)",
  },
  // SP6: controls and inner boxes are 8; a card (the outer container) is 16, so an inner
  // box never carries the same radius as the card it sits in.
  shape: { borderRadius: 8 },
  typography: {
    fontFamily:
      '"Roboto", "Helvetica", "Arial", -apple-system, BlinkMacSystemFont, sans-serif',
    // TY6: tightened tracking on display sizes. TY3: headings clearly heavier than body.
    h1: { fontWeight: 700, letterSpacing: "-0.03em" },
    h2: { fontWeight: 700, letterSpacing: "-0.025em" },
    h3: { fontWeight: 700, letterSpacing: "-0.02em" },
    h4: { fontWeight: 700, letterSpacing: "-0.02em" },
    h5: { fontWeight: 600, letterSpacing: "-0.015em" },
    h6: { fontWeight: 600, letterSpacing: "-0.01em" },
    // TY1: body text 1.5.
    body1: { lineHeight: 1.5 },
    body2: { lineHeight: 1.5 },
    caption: { lineHeight: 1.5 },
    // TY4: caps only for short eyebrow labels.
    overline: { letterSpacing: "0.08em", lineHeight: 1.6 },
    // TY4: buttons are sentences here ("Re-grade history under current"), not labels.
    button: { textTransform: "none", fontWeight: 600 },
  },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        html: { colorScheme: "dark" },
        body: { backgroundColor: SURFACE.default },
        // TY2: running text stops at ~75 characters however wide the card is.
        "p.MuiTypography-body1, p.MuiTypography-body2": { maxWidth: "75ch" },
        // DK6: a bright image is dimmed on the dark page.
        "img:not([data-keep-bright])": { filter: "brightness(0.88)" },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: { backgroundImage: "none" },
      },
    },
    MuiCard: {
      styleOverrides: {
        root: {
          backgroundImage: "none",
          border: "1px solid rgba(255,255,255,0.08)",
          borderRadius: 16,
        },
      },
    },
    MuiCardContent: {
      styleOverrides: {
        // SP5: 16px minimum; 24 on wider screens.
        root: ({ theme: t }) => ({
          padding: t.spacing(2),
          "&:last-child": { paddingBottom: t.spacing(2) },
          [t.breakpoints.up("sm")]: {
            padding: t.spacing(3),
            "&:last-child": { paddingBottom: t.spacing(3) },
          },
        }),
      },
    },
    MuiAppBar: {
      styleOverrides: {
        root: { backgroundImage: "none", backgroundColor: SURFACE.paper },
      },
    },
    MuiDrawer: {
      styleOverrides: {
        paper: { backgroundColor: SURFACE.paper },
      },
    },
    // DK4: things that float above a card are lighter than it.
    MuiPopover: {
      styleOverrides: {
        paper: { backgroundColor: SURFACE.raised, border: "1px solid rgba(255,255,255,0.08)" },
      },
    },
    MuiMenu: {
      styleOverrides: {
        paper: { backgroundColor: SURFACE.raised },
      },
    },
    MuiTooltip: {
      styleOverrides: {
        tooltip: {
          backgroundColor: SURFACE.overlay,
          color: TEXT.primary,
          fontSize: "0.8125rem",
          lineHeight: 1.5,
          maxWidth: 360,
          border: "1px solid rgba(255,255,255,0.10)",
        },
        arrow: { color: SURFACE.overlay },
      },
    },
    MuiDialog: {
      styleOverrides: {
        paper: ({ theme: t }) => ({
          backgroundColor: SURFACE.raised,
          borderRadius: 16,
          // MO6: on a phone a dialog is a bottom sheet, full width, in thumb reach.
          [t.breakpoints.down("sm")]: {
            margin: 0,
            width: "100%",
            maxWidth: "100%",
            maxHeight: "92vh",
            borderBottomLeftRadius: 0,
            borderBottomRightRadius: 0,
          },
        }),
        container: ({ theme: t }) => ({
          [t.breakpoints.down("sm")]: { alignItems: "flex-end" },
        }),
      },
    },
    MuiDialogActions: {
      styleOverrides: {
        root: ({ theme: t }) => ({
          padding: t.spacing(2, 3),
          gap: t.spacing(1),
          "& > :not(style) ~ :not(style)": { marginLeft: 0 },
          [t.breakpoints.down("sm")]: {
            flexWrap: "wrap-reverse",
            "& > .MuiButton-root": { flex: "1 1 auto" },
          },
        }),
      },
    },
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        root: { [TOUCH]: { minHeight: 44 } },
      },
    },
    MuiIconButton: {
      styleOverrides: {
        root: { [TOUCH]: { minWidth: 44, minHeight: 44 } },
      },
    },
    MuiToggleButton: {
      styleOverrides: {
        root: { textTransform: "none", [TOUCH]: { minHeight: 44 } },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: { [TOUCH]: { "&.MuiChip-clickable, &.MuiChip-deletable": { minHeight: 36 } } },
      },
    },
    MuiTab: {
      styleOverrides: {
        root: { textTransform: "none", fontWeight: 600, minHeight: 48 },
      },
    },
    MuiListItemButton: {
      styleOverrides: {
        root: { [TOUCH]: { minHeight: 44 } },
      },
    },
    // CO6: links carry an underline, not colour alone — a soft one, so a table of
    // profile names doesn't turn into a wall of rules.
    MuiLink: {
      defaultProps: { underline: "always" },
      styleOverrides: {
        root: {
          textUnderlineOffset: "0.18em",
          textDecorationThickness: "1px",
          textDecorationColor: "currentColor",
          "&.MuiLink-underlineAlways": {
            textDecorationColor: alpha(TEXT.primary, 0.4),
          },
          "&:hover": { textDecorationColor: "currentColor" },
        },
      },
    },
    // MO3: inputs stay at 16px so iPhone Safari never zooms on focus.
    MuiInputBase: {
      styleOverrides: {
        input: { fontSize: "1rem" },
      },
    },
    // CO2: an error never rests on red alone — the message gets a mark in front of it.
    MuiFormHelperText: {
      styleOverrides: {
        root: {
          lineHeight: 1.5,
          "&.Mui-error::before": { content: '"\\26A0\\FE0E\\00A0"', fontWeight: 700 },
        },
      },
    },
    MuiFormLabel: {
      styleOverrides: {
        root: { color: TEXT.secondary },
      },
    },
    // DB5: numbers line up in every table.
    MuiTableCell: {
      styleOverrides: {
        root: { fontVariantNumeric: "tabular-nums", borderColor: "rgba(255,255,255,0.08)" },
        head: { fontWeight: 600, color: TEXT.secondary },
      },
    },
    MuiAlert: {
      styleOverrides: {
        root: { alignItems: "flex-start", lineHeight: 1.5 },
      },
    },
  },
});

// Color helper for the Seat-of-Pants Score and SOPS values.
export function sopsColor(value: number | null | undefined): string {
  if (value == null) return COLORS.slate;
  if (value >= 80) return COLORS.green;
  if (value >= 50) return COLORS.amber;
  return COLORS.red;
}

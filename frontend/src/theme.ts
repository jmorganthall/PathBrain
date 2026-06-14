import { createTheme } from "@mui/material/styles";

// Dark, Material-design theme for PathBrain.
export const theme = createTheme({
  palette: {
    mode: "dark",
    primary: { main: "#4dd0e1" },
    secondary: { main: "#7c4dff" },
    success: { main: "#66bb6a" },
    warning: { main: "#ffb74d" },
    error: { main: "#ef5350" },
    background: {
      default: "#0a0e14",
      paper: "#121822",
    },
  },
  shape: { borderRadius: 12 },
  typography: {
    fontFamily:
      '"Roboto", "Helvetica", "Arial", -apple-system, BlinkMacSystemFont, sans-serif',
    h4: { fontWeight: 600 },
    h5: { fontWeight: 600 },
    h6: { fontWeight: 600 },
  },
  components: {
    MuiCard: {
      styleOverrides: {
        root: {
          backgroundImage: "none",
          border: "1px solid rgba(255,255,255,0.06)",
        },
      },
    },
    MuiAppBar: {
      styleOverrides: {
        root: {
          backgroundImage: "none",
        },
      },
    },
  },
});

// Color helper for the Seat-of-Pants Score and SOPS values.
export function sopsColor(value: number | null | undefined): string {
  if (value == null) return "#90a4ae";
  if (value >= 80) return "#66bb6a";
  if (value >= 50) return "#ffb74d";
  return "#ef5350";
}

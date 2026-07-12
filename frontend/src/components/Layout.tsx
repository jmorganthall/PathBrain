import { useEffect, useState } from "react";
import { Link as RouterLink, useLocation } from "react-router-dom";
import AppBar from "@mui/material/AppBar";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Alert from "@mui/material/Alert";
import Snackbar from "@mui/material/Snackbar";
import Tooltip from "@mui/material/Tooltip";
import Divider from "@mui/material/Divider";
import Drawer from "@mui/material/Drawer";
import IconButton from "@mui/material/IconButton";
import Link from "@mui/material/Link";
import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemIcon from "@mui/material/ListItemIcon";
import ListItemText from "@mui/material/ListItemText";
import Toolbar from "@mui/material/Toolbar";
import Typography from "@mui/material/Typography";
import useMediaQuery from "@mui/material/useMediaQuery";
import { useTheme } from "@mui/material/styles";

import MenuIcon from "@mui/icons-material/Menu";
import HubIcon from "@mui/icons-material/Hub";
import DashboardIcon from "@mui/icons-material/SpaceDashboard";
import HistoryIcon from "@mui/icons-material/Timeline";
import TrendsIcon from "@mui/icons-material/CalendarMonth";
import CompareIcon from "@mui/icons-material/CompareArrows";
import SettingsIcon from "@mui/icons-material/Tune";
import InsightsIcon from "@mui/icons-material/Insights";
import ScienceIcon from "@mui/icons-material/Science";
import ScatterPlotIcon from "@mui/icons-material/ScatterPlot";
import PowerOffIcon from "@mui/icons-material/PowerSettingsNew";
import ExtensionIcon from "@mui/icons-material/Extension";
import RuleIcon from "@mui/icons-material/Rule";
import DataObjectIcon from "@mui/icons-material/DataObject";
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome";
import SystemUpdateAltIcon from "@mui/icons-material/SystemUpdateAlt";

import type { ReactNode } from "react";

import JobStatus from "./JobStatus";
import { api, ApiError } from "../api/client";
import type { VersionInfo } from "../api/types";

const DRAWER_WIDTH = 240;

// Top-bar chip that appears only when a newer build is available to pull. Polls the
// backend's cached /api/version (hourly) so it never hammers GitHub. When one-click
// self-update is wired up (Watchtower configured → info.self_update), it also offers an
// "Update now" button that tells Watchtower to pull the new image and recreate this container.
function UpdateChip() {
  const [info, setInfo] = useState<VersionInfo | null>(null);
  // idle → triggering (POST in flight) → updating (container recreating; poll for it to return).
  const [phase, setPhase] = useState<"idle" | "triggering" | "updating">("idle");
  const [snack, setSnack] = useState<{ msg: string; sev: "info" | "error" } | null>(null);

  useEffect(() => {
    let alive = true;
    const check = () =>
      api
        .version()
        .then((v) => alive && setInfo(v))
        .catch(() => {});
    check();
    const t = setInterval(check, 60 * 60 * 1000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  // While the update is in flight the container is being recreated, so the backend goes away and
  // comes back on the new image. Poll until it answers on a *different* build (or with nothing left
  // to update), then hard-reload so the UI matches the new backend.
  useEffect(() => {
    if (phase !== "updating") return;
    const fromSha = info?.git_sha_short ?? null;
    const t = setInterval(() => {
      api
        .version()
        .then((v) => {
          if ((v.git_sha_short && v.git_sha_short !== fromSha) || !v.update_available) {
            window.location.reload();
          }
        })
        .catch(() => {}); // still restarting — keep waiting
    }, 4000);
    return () => clearInterval(t);
  }, [phase, info?.git_sha_short]);

  if (!info?.update_available) return null;
  const tip = `A newer build is available to pull (ghcr.io/jmorganthall/pathbrain:latest).\nThis build: ${
    info.git_sha_short ?? "unknown"
  } · latest: ${info.latest_sha_short ?? "?"}`;

  const onUpdate = () => {
    setPhase("triggering");
    api
      .triggerUpdate()
      .then((r) => {
        setPhase("updating");
        setSnack({ msg: r.detail || "Update triggered — PathBrain is restarting…", sev: "info" });
      })
      .catch((e) => {
        // A *successful* update severs this very request as the container is recreated, so a
        // dropped connection (ApiError status 0) means "it's happening", not a failure. Only a real
        // HTTP error from a still-alive backend (Watchtower unreachable / bad token → 502/409) is
        // surfaced as an error.
        if (e instanceof ApiError && e.status === 0) {
          setPhase("updating");
          setSnack({ msg: "Update triggered — PathBrain is restarting…", sev: "info" });
        } else {
          setPhase("idle");
          setSnack({ msg: e instanceof Error ? e.message : "Update failed to start.", sev: "error" });
        }
      });
  };

  return (
    <>
      <Tooltip title={tip}>
        <Chip
          icon={<SystemUpdateAltIcon />}
          label="Update available"
          color="warning"
          size="small"
          variant="outlined"
          component="a"
          clickable
          href={info.compare_url ?? undefined}
          target="_blank"
          rel="noopener noreferrer"
          sx={{ mr: 1 }}
        />
      </Tooltip>
      {info.self_update && (
        <Tooltip title="Pull the newer image and restart PathBrain via Watchtower">
          <span>
            <Button
              size="small"
              variant="contained"
              color="warning"
              onClick={onUpdate}
              disabled={phase !== "idle"}
              startIcon={
                phase === "idle" ? (
                  <SystemUpdateAltIcon />
                ) : (
                  <CircularProgress size={14} color="inherit" />
                )
              }
              sx={{ mr: 1 }}
            >
              {phase === "idle" ? "Update now" : phase === "triggering" ? "Starting…" : "Updating…"}
            </Button>
          </span>
        </Tooltip>
      )}
      <Snackbar
        open={!!snack}
        autoHideDuration={phase === "updating" ? null : 8000}
        onClose={() => setSnack(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        {snack ? (
          <Alert
            severity={snack.sev}
            variant="filled"
            onClose={phase === "updating" ? undefined : () => setSnack(null)}
          >
            {snack.msg}
          </Alert>
        ) : undefined}
      </Snackbar>
    </>
  );
}

// Subtle footer showing the running build, so "which version am I on?" is answerable in the UI
// (not just via /api/version). The build SHA is the key line for verifying a deploy took; the
// update-check note explains why the top-bar chip is silent when the container can't reach GitHub.
function VersionFooter() {
  const [info, setInfo] = useState<VersionInfo | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .version()
      .then((v) => alive && setInfo(v))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  // Stamped images (built by CI) carry a short SHA; a local/unstamped build shows "local".
  const build = info ? info.git_sha_short ?? "local" : "…";
  const mono = { fontFamily: "monospace", fontSize: "0.95em" } as const;
  return (
    <Box
      component="footer"
      sx={{ mt: 6, pt: 2, borderTop: 1, borderColor: "divider", textAlign: "center" }}
    >
      <Typography variant="caption" color="text.disabled">
        PathBrain{info?.version ? ` ${info.version}` : ""} · build{" "}
        <Box component="span" sx={mono}>
          {build}
        </Box>
        {info?.update_available ? (
          <>
            {" · "}
            <Link
              href={info.compare_url ?? undefined}
              target="_blank"
              rel="noopener noreferrer"
              color="warning.main"
              underline="hover"
            >
              update available ({info.latest_sha_short})
            </Link>
          </>
        ) : info?.update_check === false ? (
          " · update check disabled"
        ) : info?.error ? (
          <Tooltip title={`Update check couldn't reach GitHub: ${info.error}`}>
            <Box component="span" sx={{ cursor: "help" }}>
              {" · update check offline"}
            </Box>
          </Tooltip>
        ) : info?.latest_sha_short ? (
          " · up to date"
        ) : null}
      </Typography>
    </Box>
  );
}

interface NavItem {
  label: string;
  to: string;
  icon: ReactNode;
}

const NAV: NavItem[] = [
  { label: "Dashboard", to: "/", icon: <DashboardIcon /> },
  { label: "History", to: "/history", icon: <HistoryIcon /> },
  { label: "Trends", to: "/trends", icon: <TrendsIcon /> },
  { label: "Compare", to: "/compare", icon: <CompareIcon /> },
  { label: "Settings Impact", to: "/settings", icon: <InsightsIcon /> },
  { label: "Experiments", to: "/experiments", icon: <ScienceIcon /> },
  { label: "Shotgun Sweep", to: "/sweep", icon: <ScatterPlotIcon /> },
  { label: "Baseline (SQM off)", to: "/baseline", icon: <PowerOffIcon /> },
  { label: "Config", to: "/config", icon: <SettingsIcon /> },
  { label: "Methodology", to: "/methodology", icon: <RuleIcon /> },
  { label: "Plugins", to: "/plugins", icon: <ExtensionIcon /> },
  { label: "Data Dump", to: "/data-dump", icon: <DataObjectIcon /> },
  { label: "AI", to: "/ai", icon: <AutoAwesomeIcon /> },
];

export default function Layout({ children }: { children: ReactNode }) {
  const theme = useTheme();
  const isDesktop = useMediaQuery(theme.breakpoints.up("md"));
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();

  const isActive = (to: string) =>
    to === "/" ? location.pathname === "/" : location.pathname.startsWith(to);

  const drawerContent = (
    <Box>
      <Toolbar sx={{ gap: 1.5 }}>
        <HubIcon color="primary" />
        <Typography variant="h6" noWrap sx={{ fontWeight: 700 }}>
          PathBrain
        </Typography>
      </Toolbar>
      <Divider />
      <List sx={{ px: 1 }}>
        {NAV.map((item) => (
          <ListItemButton
            key={item.to}
            component={RouterLink}
            to={item.to}
            selected={isActive(item.to)}
            onClick={() => setMobileOpen(false)}
            sx={{ borderRadius: 2, mb: 0.5 }}
          >
            <ListItemIcon sx={{ minWidth: 40 }}>{item.icon}</ListItemIcon>
            <ListItemText primary={item.label} />
          </ListItemButton>
        ))}
      </List>
    </Box>
  );

  return (
    <Box sx={{ display: "flex", minHeight: "100vh" }}>
      <AppBar
        position="fixed"
        elevation={0}
        sx={{
          zIndex: theme.zIndex.drawer + 1,
          borderBottom: "1px solid rgba(255,255,255,0.06)",
          bgcolor: "background.paper",
        }}
      >
        <Toolbar>
          {!isDesktop && (
            <IconButton
              color="inherit"
              edge="start"
              onClick={() => setMobileOpen((v) => !v)}
              sx={{ mr: 2 }}
            >
              <MenuIcon />
            </IconButton>
          )}
          <HubIcon color="primary" sx={{ mr: 1.5, display: { md: "none" } }} />
          <Typography variant="h6" sx={{ fontWeight: 700, flexGrow: 1 }}>
            PathBrain
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: { xs: "none", sm: "block" }, mr: 1 }}>
            Network Path Intelligence
          </Typography>
          <UpdateChip />
          <JobStatus />
        </Toolbar>
      </AppBar>

      <Box
        component="nav"
        sx={{ width: { md: DRAWER_WIDTH }, flexShrink: { md: 0 } }}
      >
        <Drawer
          variant="temporary"
          open={mobileOpen}
          onClose={() => setMobileOpen(false)}
          ModalProps={{ keepMounted: true }}
          sx={{
            display: { xs: "block", md: "none" },
            "& .MuiDrawer-paper": {
              boxSizing: "border-box",
              width: DRAWER_WIDTH,
              bgcolor: "background.paper",
            },
          }}
        >
          {drawerContent}
        </Drawer>
        <Drawer
          variant="permanent"
          open
          sx={{
            display: { xs: "none", md: "block" },
            "& .MuiDrawer-paper": {
              boxSizing: "border-box",
              width: DRAWER_WIDTH,
              bgcolor: "background.paper",
              borderRight: "1px solid rgba(255,255,255,0.06)",
            },
          }}
        >
          {drawerContent}
        </Drawer>
      </Box>

      <Box
        component="main"
        sx={{
          flexGrow: 1,
          width: { md: `calc(100% - ${DRAWER_WIDTH}px)` },
          p: { xs: 2, md: 3 },
        }}
      >
        <Toolbar />
        {children}
        <VersionFooter />
      </Box>
    </Box>
  );
}

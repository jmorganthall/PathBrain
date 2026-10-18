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
import ListSubheader from "@mui/material/ListSubheader";
import Toolbar from "@mui/material/Toolbar";
import Typography from "@mui/material/Typography";
import useMediaQuery from "@mui/material/useMediaQuery";
import { useTheme } from "@mui/material/styles";

import MenuIcon from "@mui/icons-material/Menu";
import HubIcon from "@mui/icons-material/Hub";
import DashboardIcon from "@mui/icons-material/SpaceDashboard";
import OverallIcon from "@mui/icons-material/EmojiEvents";
import HistoryIcon from "@mui/icons-material/Timeline";
import TrendsIcon from "@mui/icons-material/CalendarMonth";
import CompareIcon from "@mui/icons-material/CompareArrows";
import WeatherIcon from "@mui/icons-material/Thermostat";
import SecurityIcon from "@mui/icons-material/Security";
import SettingsIcon from "@mui/icons-material/Tune";
import InsightsIcon from "@mui/icons-material/Insights";
import ScienceIcon from "@mui/icons-material/Science";
import ScatterPlotIcon from "@mui/icons-material/ScatterPlot";
import PowerOffIcon from "@mui/icons-material/PowerSettingsNew";
import FlightTakeoffIcon from "@mui/icons-material/FlightTakeoff";
import SportsMmaIcon from "@mui/icons-material/SportsMma";
import EqualizerIcon from "@mui/icons-material/Equalizer";
import ExploreIcon from "@mui/icons-material/Explore";
import ExtensionIcon from "@mui/icons-material/Extension";
import RuleIcon from "@mui/icons-material/Rule";
import DataObjectIcon from "@mui/icons-material/DataObject";
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome";
import SystemUpdateAltIcon from "@mui/icons-material/SystemUpdateAlt";

import type { ReactNode } from "react";

import ErrorBoundary from "./ErrorBoundary";
import FollowBest from "./FollowBest";
import GlossaryButton from "./Glossary";
import JobStatus from "./JobStatus";
import { api, ApiError } from "../api/client";
import type { VersionInfo } from "../api/types";

const DRAWER_WIDTH = 240;
// How long to wait for the new build to answer before calling the update a no-show. A pull +
// recreate is normally seconds; four minutes is generous for a slow registry and still short
// enough that a silent no-op gets reported instead of spinning.
const UPDATE_POLL_MS = 4 * 60 * 1000;

// Top-bar chip that appears only when a newer build is available to pull. Polls the
// backend's cached /api/version (hourly) so it never hammers GitHub. When one-click
// self-update is wired up (Watchtower configured → info.self_update), it also offers an
// "Update now" button that tells Watchtower to pull the new image and recreate this container.
function UpdateChip() {
  const [info, setInfo] = useState<VersionInfo | null>(null);
  // idle → triggering (POST in flight) → updating (container recreating; poll for it to return).
  const [phase, setPhase] = useState<"idle" | "triggering" | "updating">("idle");
  const [snack, setSnack] = useState<{ msg: string; sev: "info" | "warning" | "error" } | null>(null);

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
    // Give up eventually. Watchtower answering "accepted" never meant the build changed — if this
    // container is outside its scope, it happily accepts and updates nothing — so a poll with no
    // deadline leaves the button spinning forever on the one outcome the user most needs told
    // about. Say it plainly and point at the ledger that recorded the attempt.
    const deadline = Date.now() + UPDATE_POLL_MS;
    const t = setInterval(() => {
      if (Date.now() > deadline) {
        clearInterval(t);
        setPhase("idle");
        setSnack({
          msg:
            "Watchtower accepted the request but the build hasn't changed. See Plugins → " +
            "Watchtower → Update history for what happened.",
          sev: "warning",
        });
        return;
      }
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
          // The informational "Update available" chip is just a link to the GitHub compare. On a
          // narrow phone the toolbar can't fit both it and the action button, so when one-click
          // self-update is wired up (the button below is the real CTA), hide this chip on xs so the
          // button never gets clipped off the right edge. Shown from sm up, or always when there's
          // no button (it's the only update indicator then).
          sx={{ mr: 1, flexShrink: 0, display: info.self_update ? { xs: "none", sm: "inline-flex" } : "inline-flex" }}
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
              // Never let the action button shrink or wrap out of the toolbar on mobile.
              sx={{ mr: 1, flexShrink: 0, whiteSpace: "nowrap" }}
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
  const [checking, setChecking] = useState(false);
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
  // "Check now" forces a fresh upstream look, bypassing the 1-hour cache — so a stale "up to date"
  // can be corrected on demand instead of waiting for the cache to expire.
  const checkNow = () => {
    setChecking(true);
    api
      .refreshVersion()
      .then(setInfo)
      .catch(() => {})
      .finally(() => setChecking(false));
  };
  // Stamped images (built by CI) carry a short SHA; a local/unstamped build shows "local".
  const build = info ? info.git_sha_short ?? "local" : "…";
  const mono = { fontFamily: "monospace", fontSize: "0.95em" } as const;
  const checkedLabel = info?.checked_at
    ? new Date(info.checked_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : null;
  // Full comparison in one hover, so "up to date" is never a black box: what we run, what's latest
  // upstream, and when we last looked.
  const detail =
    info &&
    `Running ${info.git_sha_short ?? "local"} · latest on ${info.update_repo ?? "?"}@${
      info.update_branch ?? "?"
    }: ${info.latest_sha_short ?? "unknown"}${checkedLabel ? ` · checked ${checkedLabel}` : ""}`;

  const status = (() => {
    if (!info) return null;
    if (info.update_available)
      return (
        <Link
          href={info.compare_url ?? undefined}
          target="_blank"
          rel="noopener noreferrer"
          color="warning.main"
          underline="hover"
        >
          update available ({info.latest_sha_short})
        </Link>
      );
    if (info.update_check === false) return <Box component="span">update check disabled</Box>;
    if (info.error)
      return (
        <Tooltip title={`Update check couldn't reach GitHub: ${info.error}`}>
          <Box component="span" sx={{ cursor: "help" }}>
            update check offline
          </Box>
        </Tooltip>
      );
    if (info.latest_sha_short)
      return (
        <Box component="span">
          up to date <Box component="span" sx={mono}>({info.latest_sha_short})</Box>
        </Box>
      );
    return null;
  })();

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
        {status && (
          <>
            {" · "}
            {detail ? <Tooltip title={detail}><Box component="span">{status}</Box></Tooltip> : status}
          </>
        )}
        {info && info.update_check !== false && (
          <>
            {checkedLabel ? ` · checked ${checkedLabel}` : ""}{" · "}
            <Link
              component="button"
              type="button"
              onClick={() => !checking && checkNow()}
              underline="hover"
              color="inherit"
              sx={{ font: "inherit", verticalAlign: "baseline" }}
            >
              {checking ? "checking…" : "check now"}
            </Link>
          </>
        )}
      </Typography>
    </Box>
  );
}

interface NavItem {
  label: string;
  to: string;
  icon: ReactNode;
  /** One line a first-time visitor can read before clicking. Shown as the hover title. */
  what: string;
}

interface NavGroup {
  /** Section heading in the drawer; empty for the ungrouped top entry. */
  title: string;
  items: NavItem[];
}

// Twenty pages used to sit in one flat list, in roughly the order they were written. The
// groups answer the question a newcomer actually has — "where do I look for X?" — and the
// labels say what a page shows rather than what the engine behind it is called. Routes are
// unchanged; only the words are.
const NAV_GROUPS: NavGroup[] = [
  {
    title: "",
    items: [
      { label: "Dashboard", to: "/", icon: <DashboardIcon />, what: "Which profile to run, what the firewall is on, and what is measuring now." },
    ],
  },
  {
    title: "Results",
    items: [
      { label: "Best profile", to: "/overall", icon: <OverallIcon />, what: "The one ranking that combines every measurement with the head-to-head duels, and how sure it is." },
      { label: "Profile standings", to: "/settings", icon: <InsightsIcon />, what: "Every firewall settings profile ranked by its measured score, with a scatter to compare them on any metric." },
      { label: "Run history", to: "/history", icon: <HistoryIcon />, what: "Every benchmark run, newest first, with its score and the profile it measured." },
      { label: "Trends", to: "/trends", icon: <TrendsIcon />, what: "What is normal for each metric by day of week and hour of day." },
      { label: "Compare runs", to: "/compare", icon: <CompareIcon />, what: "Two or more runs side by side, metric by metric." },
      { label: "Weather", to: "/weather", icon: <WeatherIcon />, what: "How much network conditions outside the shaper's control move the scores." },
    ],
  },
  {
    title: "Find a better profile",
    items: [
      { label: "Duels", to: "/duels", icon: <SportsMmaIcon />, what: "Profiles measured head to head under the same conditions; the champion, the standings, and tonight's matches." },
      { label: "Lever duels", to: "/levers", icon: <EqualizerIcon />, what: "What moving one setting at a time does, measured in paired rounds." },
      { label: "Explore", to: "/explore", icon: <ExploreIcon />, what: "Untested settings worth measuring next, and whether past suggestions were right." },
      { label: "Shotgun sweep", to: "/sweep", icon: <ScatterPlotIcon />, what: "Try a grid of values for a setting in one go, then restore your settings." },
      { label: "Experiments", to: "/experiments", icon: <ScienceIcon />, what: "The scheduled single-setting sweep that runs in a nightly window." },
      { label: "AI suggestions", to: "/ai", icon: <AutoAwesomeIcon />, what: "Ask a language model to propose profiles from the measured data." },
    ],
  },
  {
    title: "Other tests",
    items: [
      { label: "No-shaper baseline", to: "/baseline", icon: <PowerOffIcon />, what: "Measure the link with SQM turned off, to see what the shaper is buying." },
      { label: "Away test", to: "/away", icon: <FlightTakeoffIcon />, what: "Run a test from any phone or laptop and compare it with home." },
    ],
  },
  {
    title: "System",
    items: [
      { label: "Firewall", to: "/firewall", icon: <SecurityIcon />, what: "Every write PathBrain made to the firewall and what each one cost the network." },
      { label: "Configuration", to: "/config", icon: <SettingsIcon />, what: "Targets, schedules, weights and thresholds." },
      { label: "Methodology", to: "/methodology", icon: <RuleIcon />, what: "How raw measurements become a score, versioned, and the audits that check the instrument." },
      { label: "Plugins", to: "/plugins", icon: <ExtensionIcon />, what: "The measurement plugins and the self-update integration." },
      { label: "Data export", to: "/data-dump", icon: <DataObjectIcon />, what: "Download the measured record for analysis elsewhere." },
    ],
  },
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
      <List dense sx={{ px: 1, pb: 2 }}>
        {NAV_GROUPS.map((group) => (
          <Box key={group.title || "top"} component="li" sx={{ listStyle: "none" }}>
            {group.title && (
              <ListSubheader
                disableSticky
                sx={{ bgcolor: "transparent", lineHeight: 2.2, mt: 1, px: 1.5, fontSize: 11, letterSpacing: 1, textTransform: "uppercase" }}
              >
                {group.title}
              </ListSubheader>
            )}
            <List disablePadding>
              {group.items.map((item) => (
                <Tooltip key={item.to} title={item.what} placement="right" enterDelay={600} arrow>
                  <ListItemButton
                    component={RouterLink}
                    to={item.to}
                    selected={isActive(item.to)}
                    onClick={() => setMobileOpen(false)}
                    sx={{ borderRadius: 2, mb: 0.25, py: 0.6 }}
                  >
                    <ListItemIcon sx={{ minWidth: 36 }}>{item.icon}</ListItemIcon>
                    <ListItemText primary={item.label} primaryTypographyProps={{ fontSize: 14 }} />
                  </ListItemButton>
                </Tooltip>
              ))}
            </List>
          </Box>
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
          <Typography variant="h6" noWrap sx={{ fontWeight: 700, flexGrow: 1, minWidth: 0 }}>
            PathBrain
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: { xs: "none", sm: "block" }, mr: 1 }}>
            Network Path Intelligence
          </Typography>
          {/* Each chip is boundaried on its own: they poll different endpoints, and one
              of them failing to render is one reading unavailable, not the application.
              An unboundaried chip here unmounts EVERY page, since the top bar is on all of
              them — which is exactly how a bad Arm response once blanked the whole app.
              (The firewall-guard chip stood first in this row, carrying Arm and Hands off.
              It is gone with the state it controlled; the write rate and the ledger live on
              the Firewall page, which is where you go to read them.) */}
          <ErrorBoundary label="Glossary" variant="widget"><GlossaryButton /></ErrorBoundary>
          <ErrorBoundary label="Auto-apply" variant="widget"><FollowBest /></ErrorBoundary>
          <ErrorBoundary label="Update" variant="widget"><UpdateChip /></ErrorBoundary>
          <ErrorBoundary label="Jobs" variant="widget"><JobStatus /></ErrorBoundary>
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
          // A flex child defaults to min-width:auto, so one wide descendant (a standings
          // table, a long row of chips) makes the whole column wider than the phone and
          // pushes every card's text off-screen. Pinning min-width to 0 lets the column
          // shrink to the viewport and leaves the wide thing to scroll inside its own
          // container, which is where the horizontal scroll belongs.
          minWidth: 0,
          maxWidth: "100%",
          overflowX: "hidden",
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

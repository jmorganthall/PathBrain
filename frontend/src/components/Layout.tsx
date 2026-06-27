import { useEffect, useState } from "react";
import { Link as RouterLink, useLocation } from "react-router-dom";
import AppBar from "@mui/material/AppBar";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Tooltip from "@mui/material/Tooltip";
import Divider from "@mui/material/Divider";
import Drawer from "@mui/material/Drawer";
import IconButton from "@mui/material/IconButton";
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
import ExtensionIcon from "@mui/icons-material/Extension";
import RuleIcon from "@mui/icons-material/Rule";
import DataObjectIcon from "@mui/icons-material/DataObject";
import SystemUpdateAltIcon from "@mui/icons-material/SystemUpdateAlt";

import type { ReactNode } from "react";

import JobStatus from "./JobStatus";
import { api } from "../api/client";
import type { VersionInfo } from "../api/types";

const DRAWER_WIDTH = 240;

// Top-bar chip that appears only when a newer build is available to pull. Polls the
// backend's cached /api/version (hourly) so it never hammers GitHub.
function UpdateChip() {
  const [info, setInfo] = useState<VersionInfo | null>(null);
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
  if (!info?.update_available) return null;
  const tip = `A newer build is available to pull (ghcr.io/jmorganthall/pathbrain:latest).\nThis build: ${
    info.git_sha_short ?? "unknown"
  } · latest: ${info.latest_sha_short ?? "?"}`;
  return (
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
  { label: "Config", to: "/config", icon: <SettingsIcon /> },
  { label: "Methodology", to: "/methodology", icon: <RuleIcon /> },
  { label: "Plugins", to: "/plugins", icon: <ExtensionIcon /> },
  { label: "Data Dump", to: "/data-dump", icon: <DataObjectIcon /> },
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
      </Box>
    </Box>
  );
}

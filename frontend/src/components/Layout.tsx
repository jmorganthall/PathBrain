import { useState } from "react";
import { Link as RouterLink, useLocation } from "react-router-dom";
import AppBar from "@mui/material/AppBar";
import Box from "@mui/material/Box";
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

import type { ReactNode } from "react";

const DRAWER_WIDTH = 240;

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
          <Typography variant="caption" color="text.secondary">
            Network Path Intelligence
          </Typography>
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

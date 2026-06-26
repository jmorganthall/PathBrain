import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import History from "./pages/History";
import Trends from "./pages/Trends";
import ShotgunSweep from "./pages/ShotgunSweep";
import RunDetail from "./pages/RunDetail";
import Compare from "./pages/Compare";
import Config from "./pages/Config";
import Plugins from "./pages/Plugins";
import Settings from "./pages/Settings";
import Experiments from "./pages/Experiments";

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/history" element={<History />} />
        <Route path="/trends" element={<Trends />} />
        <Route path="/runs/:id" element={<RunDetail />} />
        <Route path="/compare" element={<Compare />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/experiments" element={<Experiments />} />
        <Route path="/sweep" element={<ShotgunSweep />} />
        <Route path="/config" element={<Config />} />
        <Route path="/plugins" element={<Plugins />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}

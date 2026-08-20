// Route table — every page is code-split.
//
// The app was one 1.2 MB chunk, so opening ANY page first downloaded, parsed and executed
// every other page plus the charting library (recharts, which only three views use). On a
// phone that was over a second of dead screen before a single request was even sent —
// which read as "the page is slow" when the API had barely been asked anything yet.
//
// `lazy` + `Suspense` gives each route its own chunk: the shell paints immediately, the
// page you asked for arrives on its own, and the chart code only loads for views that
// actually draw charts.
import { Suspense, lazy } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Loading from "./components/Loading";

const Dashboard = lazy(() => import("./pages/Dashboard"));
const History = lazy(() => import("./pages/History"));
const Trends = lazy(() => import("./pages/Trends"));
const ShotgunSweep = lazy(() => import("./pages/ShotgunSweep"));
const RunDetail = lazy(() => import("./pages/RunDetail"));
const Compare = lazy(() => import("./pages/Compare"));
const Config = lazy(() => import("./pages/Config"));
const Plugins = lazy(() => import("./pages/Plugins"));
const Methodology = lazy(() => import("./pages/Methodology"));
const Settings = lazy(() => import("./pages/Settings"));
const ProfileDetail = lazy(() => import("./pages/ProfileDetail"));
const Experiments = lazy(() => import("./pages/Experiments"));
const DataDump = lazy(() => import("./pages/DataDump"));
const AI = lazy(() => import("./pages/AI"));
const Baseline = lazy(() => import("./pages/Baseline"));
const Duels = lazy(() => import("./pages/Duels"));

export default function App() {
  return (
    <Layout>
      <Suspense fallback={<Loading />}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/history" element={<History />} />
          <Route path="/trends" element={<Trends />} />
          <Route path="/runs/:id" element={<RunDetail />} />
          <Route path="/compare" element={<Compare />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/profiles/:fingerprint" element={<ProfileDetail />} />
          <Route path="/experiments" element={<Experiments />} />
          <Route path="/sweep" element={<ShotgunSweep />} />
          <Route path="/duels" element={<Duels />} />
          <Route path="/baseline" element={<Baseline />} />
          <Route path="/config" element={<Config />} />
          <Route path="/methodology" element={<Methodology />} />
          <Route path="/plugins" element={<Plugins />} />
          <Route path="/data-dump" element={<DataDump />} />
          <Route path="/ai" element={<AI />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </Layout>
  );
}

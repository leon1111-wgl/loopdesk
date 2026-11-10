import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { isStaff, useAuth } from "./auth";
import Layout from "./components/Layout";
import ApprovalsPage from "./pages/ApprovalsPage";
import AuditPage from "./pages/AuditPage";
import ChatPage from "./pages/ChatPage";
import DashboardPage from "./pages/DashboardPage";
import EvalsPage from "./pages/EvalsPage";
import FlywheelPage from "./pages/FlywheelPage";
import HandoffsPage from "./pages/HandoffsPage";
import IntroPage from "./pages/IntroPage";
import { SKIP_INTRO_KEY } from "./components/ui";
import KnowledgePage from "./pages/KnowledgePage";
import LoginPage from "./pages/LoginPage";
import RefundsPage from "./pages/RefundsPage";
import { lazy, Suspense } from "react";
import TicketsPage from "./pages/TicketsPage";
import ToolsPage from "./pages/ToolsPage";
import TracesPage from "./pages/TracesPage";

// The simulations page carries the recorded transcripts: loaded only when opened.
const SimulationsPage = lazy(() => import("./pages/SimulationsPage"));
// The policy page loads the policy engine and, on demand, the recorded transcripts it replays.
const PolicyPage = lazy(() => import("./pages/PolicyPage"));

export default function App() {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <div className="empty">加载中…</div>;
  // The intro page is the root for everyone, signed in or not (it shows who is signed in and opens
  // their console); the "下次直接进入" choice skips it. /login and deep links are unchanged.
  let skip = false;
  try {
    skip = localStorage.getItem(SKIP_INTRO_KEY) === "1";
  } catch {
    /* storage unavailable: show the intro */
  }
  if (location.pathname === "/" && !skip) return <IntroPage />;
  if (!user) return <LoginPage />;
  const staff = isStaff(user);
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/chat/:conversationId" element={<ChatPage />} />
        <Route path="/simulations" element={<Suspense fallback={<div className="empty">加载中…</div>}><SimulationsPage /></Suspense>} />
        {staff && (
          <>
            <Route path="/dashboard" element={<DashboardPage />} />
            <Route path="/approvals" element={<ApprovalsPage />} />
            <Route path="/handoffs" element={<HandoffsPage />} />
            <Route path="/tickets" element={<TicketsPage />} />
            <Route path="/refunds" element={<RefundsPage />} />
            <Route path="/knowledge" element={<KnowledgePage />} />
            <Route path="/traces" element={<TracesPage />} />
            <Route path="/traces/:runId" element={<TracesPage />} />
            <Route path="/tools" element={<ToolsPage />} />
            <Route path="/policy" element={<Suspense fallback={<div className="empty">加载中…</div>}><PolicyPage /></Suspense>} />
            <Route path="/flywheel" element={<FlywheelPage />} />
            <Route path="/evals" element={<EvalsPage />} />
            {user.role === "admin" && <Route path="/audit" element={<AuditPage />} />}
          </>
        )}
        <Route path="*" element={<Navigate to={staff ? "/dashboard" : "/chat"} replace />} />
      </Route>
    </Routes>
  );
}

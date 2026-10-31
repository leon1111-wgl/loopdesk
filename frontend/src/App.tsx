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
import IntroPage, { SKIP_INTRO_KEY } from "./pages/IntroPage";
import KnowledgePage from "./pages/KnowledgePage";
import LoginPage from "./pages/LoginPage";
import RefundsPage from "./pages/RefundsPage";
import TicketsPage from "./pages/TicketsPage";
import TracesPage from "./pages/TracesPage";

export default function App() {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <div className="empty">加载中…</div>;
  if (!user) {
    // The intro page at the root (with the demo accounts on it); /login, any deep link, or the
    // "下次直接进入" choice go straight to sign-in.
    let skip = false;
    try {
      skip = localStorage.getItem(SKIP_INTRO_KEY) === "1";
    } catch {
      /* storage unavailable: show the intro */
    }
    return location.pathname === "/" && !skip ? <IntroPage /> : <LoginPage />;
  }
  const staff = isStaff(user);
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/chat/:conversationId" element={<ChatPage />} />
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

import { Navigate } from "react-router-dom";
import type { RouteObject } from "react-router-dom";
import SettingsPage from "../pages/Settings";
import ChatPage from "../pages/chat/ChatPage";
import AgentsPage from "../pages/agents/AgentsPage";
import ReportsPage from "../pages/reports/ReportsPage";
import ControlCenterPage from "../pages/control/ControlCenter";
import TradingPage from "../pages/trading/TradingPage";
import ApprovalCenterPage from "../pages/approvals/ApprovalCenter";
import { ModulePlaceholder } from "../pages/Placeholder";
import { NAV_ITEMS } from "../config/nav";

/** 主布局下的受控子路由：13 导航模块 + 重定向兜底（spec-06 §4）。 */
export const protectedChildren: RouteObject[] = [
  { index: true, element: <Navigate to="/chat" replace /> },
  ...NAV_ITEMS.filter((n) => n.key !== "settings").map((n) => ({
    path: n.path,
    element:
      n.key === "chat" ? (
        <ChatPage />
      ) : n.key === "agents" ? (
        <AgentsPage />
      ) : n.key === "control" ? (
        <ControlCenterPage />
      ) : n.key === "trading" ? (
        <TradingPage />
      ) : n.key === "reports" ? (
        <ReportsPage />
      ) : n.key === "approvals" ? (
        <ApprovalCenterPage />
      ) : (
        <ModulePlaceholder nav={n} />
      ),
  })),
  { path: "/settings", element: <SettingsPage /> },
  { path: "*", element: <Navigate to="/chat" replace /> },
];

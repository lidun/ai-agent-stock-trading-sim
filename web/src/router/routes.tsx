import { Navigate } from "react-router-dom";
import type { RouteObject } from "react-router-dom";
import SettingsPage from "../pages/Settings";
import { ModulePlaceholder, ChatPlaceholder } from "../pages/Placeholder";
import { NAV_ITEMS } from "../config/nav";

/** 主布局下的受控子路由：13 导航模块 + 重定向兜底（spec-06 §4）。 */
export const protectedChildren: RouteObject[] = [
  { index: true, element: <Navigate to="/chat" replace /> },
  ...NAV_ITEMS.filter((n) => n.key !== "settings").map((n) => ({
    path: n.path,
    element:
      n.key === "chat" ? <ChatPlaceholder /> : <ModulePlaceholder nav={n} />,
  })),
  { path: "/settings", element: <SettingsPage /> },
  { path: "*", element: <Navigate to="/chat" replace /> },
];

import { useMemo } from "react";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { ConfigProvider, App as AntApp, theme as antTheme } from "antd";
import zhCN from "antd/locale/zh_CN";
import AuthGate from "./components/AuthGate";
import MainLayout from "./layouts/MainLayout";
import LoginPage from "./pages/Login";
import { protectedChildren } from "./router/routes";

const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    element: <AuthGate />,
    children: [
      {
        path: "/",
        element: <MainLayout />,
        children: protectedChildren,
      },
    ],
  },
]);

export default function App() {
  const prefersDark = useMemo(
    () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false,
    [],
  );

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: prefersDark ? antTheme.darkAlgorithm : antTheme.defaultAlgorithm,
        token: { borderRadius: 6 },
      }}
    >
      <AntApp>
        <RouterProvider router={router} />
      </AntApp>
    </ConfigProvider>
  );
}

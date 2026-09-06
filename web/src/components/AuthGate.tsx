import { useEffect } from "react";
import { Spin } from "antd";
import { Outlet, Navigate } from "react-router-dom";
import { useAuth } from "../store/auth";

/** 路由守卫：应用启动先探测会话（me），未登录跳转 /login。 */
export default function AuthGate() {
  const booting = useAuth((s) => s.booting);
  const user = useAuth((s) => s.user);
  const boot = useAuth((s) => s.boot);

  useEffect(() => {
    if (booting) void boot();
  }, [booting, boot]);

  if (booting) {
    return (
      <div style={{ height: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <Spin size="large" tip="正在载入…">
          <div style={{ width: 240, height: 40 }} />
        </Spin>
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
  return <Outlet />;
}

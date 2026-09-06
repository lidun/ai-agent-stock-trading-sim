import { useState } from "react";
import { Form, Input, Button, Alert, Card, Typography } from "antd";
import { LockOutlined, UserOutlined, LineChartOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { ensureCsrf } from "../api/client";
import { login as apiLogin, fetchMe } from "../api/endpoints";
import { useAuth } from "../store/auth";

export default function LoginPage() {
  const navigate = useNavigate();
  const setUser = useAuth((s) => s.setUser);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onFinish = async (values: { username: string; password: string }) => {
    setSubmitting(true);
    setError(null);
    try {
      await ensureCsrf();
      await apiLogin(values.username, values.password);
      const me = await fetchMe();
      setUser(me);
      navigate("/", { replace: true });
    } catch (e) {
      const err = e as { status?: number; message?: string };
      setError(err.message ?? "登录失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{
        minHeight: "100%",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
        background: "linear-gradient(180deg,#001529 0%,#00284d 55%,#0b3a2e 100%)",
      }}
    >
      <div style={{ marginBottom: 24, textAlign: "center", color: "#fff" }}>
        <LineChartOutlined style={{ fontSize: 44, color: "#40a9ff" }} />
        <Typography.Title level={3} style={{ color: "#fff", margin: "12px 0 4px" }}>
          AI Agent Trading
        </Typography.Title>
        <Typography.Text style={{ color: "rgba(255,255,255,0.65)" }}>
          多 Agent 投资操作系统的单用户入口
        </Typography.Text>
      </div>
      <Card style={{ width: 360, boxShadow: "0 8px 24px rgba(0,0,0,0.25)" }}>
        {error && (
          <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} closable />
        )}
        <Form name="login" onFinish={onFinish} size="large">
          <Form.Item name="username" rules={[{ required: true, message: "请输入用户名" }]}>
            <Input prefix={<UserOutlined />} placeholder="用户名" autoComplete="username" />
          </Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: "请输入密码" }]}>
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="密码"
              autoComplete="current-password"
            />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0 }}>
            <Button type="primary" htmlType="submit" block loading={submitting}>
              登 录
            </Button>
          </Form.Item>
        </Form>
      </Card>
      <Typography.Text style={{ marginTop: 16, color: "rgba(255,255,255,0.4)", fontSize: 12 }}>
        运行于 127.0.0.1 环回 · SQLite 单机存储 · 支持在线备份
      </Typography.Text>
    </div>
  );
}

import { useMemo, useState } from "react";
import {
  Layout,
  Menu,
  Dropdown,
  Avatar,
  Typography,
  Button,
  Modal,
  Form,
  Input,
  App as AntApp,
  Tag,
  Tooltip,
  theme as antTheme,
} from "antd";
import {
  UserOutlined,
  LogoutOutlined,
  LockOutlined,
  DownOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
} from "@ant-design/icons";
import { Outlet, useLocation, useNavigate, useMatches } from "react-router-dom";
import { NAV_ITEMS } from "../config/nav";
import { useAuth } from "../store/auth";
import { useConnection } from "../hooks/useConnection";
import { changePassword, logout as apiLogout } from "../api/endpoints";

function ConnTag() {
  const state = useConnection();
  const map = {
    connecting: { color: "processing", text: "连接中" },
    online: { color: "success", text: "已连接" },
    offline: { color: "error", text: "连接中断，自动重连中" },
  } as const;
  const m = map[state];
  return (
    <Tooltip title="与后端实时通道（行情/任务推送）">
      <Tag color={m.color} style={{ marginInlineEnd: 12 }}>
        {m.text}
      </Tag>
    </Tooltip>
  );
}

function ChangePasswordModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const [submitting, setSubmitting] = useState(false);

  const submit = async (values: {
    current_password: string;
    new_password: string;
  }) => {
    setSubmitting(true);
    try {
      await changePassword(values.current_password, values.new_password);
      message.success("口令已修改");
      form.resetFields();
      onClose();
    } catch (e) {
      message.error((e as Error).message ?? "修改失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="修改口令"
      open={open}
      onCancel={onClose}
      footer={null}
      destroyOnClose
    >
      <Form form={form} layout="vertical" onFinish={submit}>
        <Form.Item
          name="current_password"
          label="当前口令"
          rules={[{ required: true, message: "请输入当前口令" }]}
        >
          <Input.Password autoComplete="current-password" />
        </Form.Item>
        <Form.Item
          name="new_password"
          label="新口令"
          rules={[
            { required: true, message: "请输入新口令" },
            { min: 8, message: "至少 8 位" },
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          name="confirm"
          label="确认新口令"
          dependencies={["new_password"]}
          rules={[
            { required: true, message: "请再次输入新口令" },
            ({ getFieldValue }) => ({
              validator(_, value) {
                if (!value || getFieldValue("new_password") === value) {
                  return Promise.resolve();
                }
                return Promise.reject(new Error("两次输入不一致"));
              },
            }),
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Button type="primary" htmlType="submit" block loading={submitting}>
          保存
        </Button>
      </Form>
    </Modal>
  );
}

export default function MainLayout() {
  const { token } = antTheme.useToken();
  const navigate = useNavigate();
  const location = useLocation();
  const matches = useMatches();
  const user = useAuth((s) => s.user);
  const setUser = useAuth((s) => s.setUser);
  const { message } = AntApp.useApp();
  const [collapsed, setCollapsed] = useState(false);
  const [pwdOpen, setPwdOpen] = useState(false);

  const selectedKey = useMemo(() => {
    const deepest = matches[matches.length - 1];
    const key = (deepest?.pathname ?? location.pathname).split("/")[1] || "chat";
    const hit = NAV_ITEMS.find((n) => n.key === key);
    return hit ? key : "chat";
  }, [matches, location.pathname]);

  const onLogout = async () => {
    try {
      await apiLogout();
    } catch {
      /* 忽略服务端登出失败，仍然回登录页 */
    }
    setUser(null);
    navigate("/login", { replace: true });
    message.success("已退出登录");
  };

  const menuItems = NAV_ITEMS.map((n) => ({
    key: n.key,
    icon: n.icon,
    label: n.label,
    onClick: () => navigate(n.path),
  }));

  return (
    <Layout style={{ minHeight: "100%" }}>
      <Layout.Sider
        collapsible
        collapsed={collapsed}
        trigger={null}
        width={208}
        theme="dark"
        style={{ position: "sticky", top: 0, height: "100vh", overflow: "auto" }}
      >
        <div
          onClick={() => navigate("/")}
          style={{
            height: 48,
            margin: 8,
            borderRadius: 8,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            cursor: "pointer",
            color: "#fff",
            fontWeight: 700,
            fontSize: collapsed ? 16 : 15,
            letterSpacing: 0.5,
            background: "rgba(255,255,255,0.08)",
            whiteSpace: "nowrap",
            overflow: "hidden",
          }}
        >
          {collapsed ? "AAT" : "AI Agent Trading"}
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={menuItems}
          style={{ borderInlineEnd: "none" }}
        />
      </Layout.Sider>
      <Layout>
        <Layout.Header
          style={{
            background: token.colorBgContainer,
            padding: "0 16px",
            display: "flex",
            alignItems: "center",
            gap: 8,
            height: 48,
            lineHeight: "48px",
            borderBottom: `1px solid ${token.colorSplit}`,
            position: "sticky",
            top: 0,
            zIndex: 10,
          }}
        >
          <Button
            type="text"
            icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            onClick={() => setCollapsed((c) => !c)}
          />
          <Typography.Text strong style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>
            {NAV_ITEMS.find((n) => n.key === selectedKey)?.label ?? ""}
          </Typography.Text>
          <ConnTag />
          <Dropdown
            menu={{
              items: [
                { key: "pwd", icon: <LockOutlined />, label: "修改口令" },
                { type: "divider" },
                { key: "logout", icon: <LogoutOutlined />, label: "退出登录", danger: true },
              ],
              onClick: ({ key }) => {
                if (key === "pwd") setPwdOpen(true);
                else if (key === "logout") void onLogout();
              },
            }}
          >
            <span style={{ cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6 }}>
              <Avatar size={26} icon={<UserOutlined />} style={{ background: "#1677ff" }} />
              <span style={{ maxWidth: 120, overflow: "hidden", textOverflow: "ellipsis" }}>
                {user?.user ?? "…"}
              </span>
              <DownOutlined style={{ fontSize: 10, opacity: 0.6 }} />
            </span>
          </Dropdown>
        </Layout.Header>
        <Layout.Content style={{ padding: 0 }}>
          <Outlet />
        </Layout.Content>
        <Layout.Footer style={{ textAlign: "center", color: token.colorTextTertiary, padding: "12px 0" }}>
          AI Agent Trading v0.1 · 单用户本地部署 · core {user?.core_version ?? "—"}
        </Layout.Footer>
      </Layout>
      <ChangePasswordModal open={pwdOpen} onClose={() => setPwdOpen(false)} />
    </Layout>
  );
}

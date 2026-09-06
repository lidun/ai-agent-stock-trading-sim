import { useEffect, useState } from "react";
import {
  Card,
  Descriptions,
  List,
  Button,
  Tag,
  Typography,
  App as AntApp,
  Space,
} from "antd";
import { ReloadOutlined, SafetyOutlined } from "@ant-design/icons";
import { useAuth } from "../store/auth";
import { useConnection } from "../hooks/useConnection";
import {
  listSessions,
  revokeSession,
  fetchHealth,
  type SessionInfo,
  type Health,
} from "../api/endpoints";
import { EmptyState } from "../components/EmptyState";

const CONN_LABEL: Record<string, string> = {
  connecting: "连接中…",
  online: "已连接",
  offline: "中断重连中",
};

export default function SettingsPage() {
  const user = useAuth((s) => s.user);
  const conn = useConnection();
  const { message } = AntApp.useApp();
  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);
  const [currentHash, setCurrentHash] = useState<string>("");
  const [health, setHealth] = useState<Health | null>(null);
  const [revoking, setRevoking] = useState(false);

  const load = async () => {
    try {
      const [s, h] = await Promise.all([listSessions(), fetchHealth()]);
      setSessions(s.sessions);
      setCurrentHash(s.current_token_hash);
      setHealth(h);
    } catch (e) {
      message.error((e as Error).message ?? "加载失败");
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const revoke = async (h: string) => {
    setRevoking(true);
    try {
      await revokeSession(h);
      message.success("已强制下线该会话");
      await load();
    } catch (e) {
      message.error((e as Error).message ?? "操作失败");
    } finally {
      setRevoking(false);
    }
  };

  return (
    <div className="page">
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        设置
      </Typography.Title>
      <Space direction="vertical" size={16} style={{ width: "100%", maxWidth: 860 }}>
        <Card title="系统状态">
          {health ? (
            <Descriptions column={{ xs: 1, sm: 2 }} size="small" bordered>
              <Descriptions.Item label="服务">
                {health.service} {health.version}
              </Descriptions.Item>
              <Descriptions.Item label="运行环境">{health.env}</Descriptions.Item>
              <Descriptions.Item label="实时通道">{CONN_LABEL[conn]}</Descriptions.Item>
              <Descriptions.Item label="SQLite 健康">
                {health.db ? <Tag color="success">正常</Tag> : <Tag color="error">异常</Tag>}
              </Descriptions.Item>
              <Descriptions.Item label="单实例锁">{String(health.single_instance)}</Descriptions.Item>
              <Descriptions.Item label="已运行">{health.uptime_s}s</Descriptions.Item>
            </Descriptions>
          ) : (
            <EmptyState title="加载中…" icon={<ReloadOutlined />} />
          )}
        </Card>

        <Card title="账户">
          <Descriptions column={1} size="small">
            <Descriptions.Item label="用户名">{user?.user}</Descriptions.Item>
            <Descriptions.Item label="登录于（京）">
              <span className="zone-bj">{user?.login_at ?? "—"}</span>
            </Descriptions.Item>
            <Descriptions.Item label="口令">
              <Typography.Text type="secondary">
                修改口令请使用右上角账户菜单
              </Typography.Text>
            </Descriptions.Item>
          </Descriptions>
        </Card>

        <Card
          title={
            <Space>
              <SafetyOutlined />
              登录会话
            </Space>
          }
          extra={<Button size="small" onClick={() => void load()}>刷新</Button>}
        >
          {sessions === null ? (
            <EmptyState title="加载中…" />
          ) : sessions.length === 0 ? (
            <EmptyState title="暂无会话" />
          ) : (
            <List
              size="small"
              dataSource={sessions}
              renderItem={(s) => (
                <List.Item
                  actions={
                    s.token_hash === currentHash
                      ? []
                      : [
                          <Button
                            key="revoke"
                            size="small"
                            danger
                            loading={revoking}
                            disabled={Boolean(s.revoked)}
                            onClick={() => void revoke(s.token_hash)}
                          >
                            {s.revoked ? "已下线" : "强制下线"}
                          </Button>,
                        ]
                  }
                >
                  <List.Item.Meta
                    title={
                      <Space>
                        {s.token_hash === currentHash && <Tag color="blue">当前会话</Tag>}
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {s.ip}
                        </Typography.Text>
                      </Space>
                    }
                    description={
                      <Typography.Text
                        type="secondary"
                        style={{ fontSize: 12, display: "block" }}
                        ellipsis
                      >
                        {s.user_agent} · 登录 {s.created_at} · 最近活跃 {s.last_seen_at} · 过期于{" "}
                        {s.expires_at}
                      </Typography.Text>
                    }
                  />
                </List.Item>
              )}
            />
          )}
        </Card>
      </Space>
    </div>
  );
}

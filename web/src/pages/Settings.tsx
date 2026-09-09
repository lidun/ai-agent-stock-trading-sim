import { useEffect, useState } from "react";
import {
  Button,
  Card,
  Descriptions,
  Flex,
  Form,
  Input,
  List,
  Select,
  Space,
  Tag,
  Typography,
  App as AntApp,
} from "antd";
import {
  ApiOutlined,
  ReloadOutlined,
  SafetyOutlined,
  ThunderboltOutlined,
  DeleteOutlined,
} from "@ant-design/icons";
import { useAuth } from "../store/auth";
import { useConnection } from "../hooks/useConnection";
import {
  fetchLlmProvider,
  fetchHealth,
  listSessions,
  removeLlmProvider,
  revokeSession,
  saveLlmProvider,
  testLlmProvider,
  type Health,
  type LlmProviderView,
  type SessionInfo,
} from "../api/endpoints";
import { EmptyState } from "../components/EmptyState";

const CONN_LABEL: Record<string, string> = {
  connecting: "连接中…",
  online: "已连接",
  offline: "中断重连中",
};

interface Vendor {
  key: string;
  label: string;
  base_url: string;
  model: string;
}

const LLM_VENDORS: Vendor[] = [
  { key: "deepseek", label: "DeepSeek", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  { key: "openai", label: "OpenAI", base_url: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  { key: "dashscope", label: "通义千问（DashScope 兼容）", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" },
  { key: "moonshot", label: "Kimi（Moonshot）", base_url: "https://api.moonshot.cn/v1", model: "moonshot-v1-8k" },
  { key: "zhipu", label: "智谱 GLM", base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-flash" },
];

function vendorByKey(key: string): Vendor {
  return LLM_VENDORS.find((v) => v.key === key) ?? LLM_VENDORS[0];
}

export default function SettingsPage() {
  const user = useAuth((s) => s.user);
  const conn = useConnection();
  const { message, modal } = AntApp.useApp();
  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);
  const [currentHash, setCurrentHash] = useState<string>("");
  const [health, setHealth] = useState<Health | null>(null);
  const [revoking, setRevoking] = useState(false);

  const [llm, setLlm] = useState<LlmProviderView | null>(null);
  const [vendor, setVendor] = useState<string>("deepseek");
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);

  const applyLlm = (v: LlmProviderView) => {
    setLlm(v);
    setVendor(v.preset || "deepseek");
    setApiKey("");
  };

  const load = async () => {
    try {
      const [s, h, ll] = await Promise.all([listSessions(), fetchHealth(), fetchLlmProvider()]);
      setSessions(s.sessions);
      setCurrentHash(s.current_token_hash);
      setHealth(h);
      applyLlm(ll);
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

  const saveProvider = async () => {
    const v = vendorByKey(vendor);
    setSaving(true);
    try {
      const res = await saveLlmProvider({
        preset: vendor,
        base_url: v.base_url,
        model: v.model,
        ...(apiKey ? { api_key: apiKey.trim() } : {}),
      });
      applyLlm(res.provider);
      message.success(res.provider.api_key_set ? "模型服务已保存（密钥加密落库）" : "模型服务已保存");
    } catch (e) {
      message.error((e as Error).message ?? "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const runTest = async () => {
    setTesting(true);
    try {
      const res = await testLlmProvider();
      if (res.ok) {
        message.success(`连通成功 · ${res.model ?? vendorByKey(vendor).model}（${res.latency_ms ?? "?"}ms）`);
      } else {
        message.error(res.error ?? "连通失败");
      }
      await load();
    } catch (e) {
      message.error((e as Error).message ?? "测试失败");
    } finally {
      setTesting(false);
    }
  };

  const removeProvider = () => {
    modal.confirm({
      title: "移除模型服务配置",
      content: "将删除已加密保存的 API Key 与厂商配置。确认移除？",
      okButtonProps: { danger: true },
      okText: "移除",
      onOk: async () => {
        try {
          const res = await removeLlmProvider();
          applyLlm(res.provider);
          message.success("已移除模型服务配置");
          await load();
        } catch (e) {
          message.error((e as Error).message ?? "移除失败");
        }
      },
    });
  };

  const auto = vendorByKey(vendor);
  const keepExistingKey = Boolean(llm?.api_key_set && vendor === llm?.preset);
  const canSave = Boolean(apiKey.trim() || keepExistingKey);

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
              <Descriptions.Item label="模型服务">
                {health.llm?.configured ? (
                  <Space size={4}>
                    <Tag color="blue">{health.llm.model}</Tag>
                    {health.llm.last_test ? (
                      health.llm.last_test.ok ? (
                        <Tag color="success">最近自检通过</Tag>
                      ) : (
                        <Tag color="error">最近自检失败</Tag>
                      )
                    ) : null}
                  </Space>
                ) : (
                  <Tag>未配置</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="单实例锁">{String(health.single_instance)}</Descriptions.Item>
              <Descriptions.Item label="已运行">{health.uptime_s}s</Descriptions.Item>
            </Descriptions>
          ) : (
            <EmptyState title="加载中…" icon={<ReloadOutlined />} />
          )}
        </Card>

        <Card
          title={
            <Space>
              <ApiOutlined />
              模型服务
            </Space>
          }
          extra={
            llm?.configured ? (
              <Space size={4}>
                <Tag color="blue">已配置</Tag>
                {llm.api_key_set && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    密钥 {llm.api_key_hint}
                  </Typography.Text>
                )}
              </Space>
            ) : (
              <Tag>未配置</Tag>
            )
          }
        >
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            选择厂商并填入你的 API Key 即可，端点与模型自动采用该厂商默认值（OpenAI 兼容统一适配层，spec-04 §8）。
            Key 在 core 本地加密落库，界面仅掩码展示。
          </Typography.Paragraph>
          <Form layout="vertical" size="small" style={{ maxWidth: 560 }}>
            <Form.Item label="厂商">
              <Select
                value={vendor}
                options={LLM_VENDORS.map((v) => ({ value: v.key, label: v.label }))}
                onChange={(k) => {
                  setVendor(k);
                  setApiKey("");
                }}
              />
            </Form.Item>
            <Form.Item label="API Key">
              <Input.Password
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={llm?.api_key_set && vendor === llm?.preset ? "留空保留原密钥，输入则替换" : "粘贴该厂商的 API Key"}
                autoComplete="new-password"
              />
            </Form.Item>
            <Form.Item style={{ marginBottom: 0 }}>
              <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 8 }}>
                将自动使用端点 {auto.base_url} · 模型 {auto.model}
              </Typography.Text>
              <Flex gap={8} wrap>
                <Button type="primary" loading={saving} disabled={!canSave} onClick={() => void saveProvider()}>
                  保存
                </Button>
                <Button icon={<ThunderboltOutlined />} loading={testing} disabled={!llm?.configured} onClick={() => void runTest()}>
                  测试连通
                </Button>
                <Button danger ghost icon={<DeleteOutlined />} disabled={!llm?.api_key_set && !llm?.configured} onClick={removeProvider}>
                  移除配置
                </Button>
                {llm?.last_test && (
                  <Typography.Text
                    type={llm.last_test.ok ? "success" : "danger"}
                    style={{ fontSize: 12, alignSelf: "center" }}
                    ellipsis
                  >
                    {llm.last_test.ok
                      ? `最近自检通过（${(llm.last_test.ts ?? "").replace("T", " ").slice(0, 19) || "—"}）`
                      : `最近自检失败：${llm.last_test.error ?? "未知错误"}`}
                  </Typography.Text>
                )}
              </Flex>
            </Form.Item>
          </Form>
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

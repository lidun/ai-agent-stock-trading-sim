import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  App as AntApp,
  Avatar,
  Badge,
  Button,
  Card,
  Empty,
  Skeleton,
  Tag,
  Tooltip,
  Typography,
  theme as antTheme,
} from "antd";
import {
  CrownOutlined,
  LoadingOutlined,
  MessageOutlined,
  RobotOutlined,
} from "@ant-design/icons";
import {
  listAgents,
  listConversations,
  type AgentInfo,
  type ConversationInfo,
} from "../../api/endpoints";
import { useConnection } from "../../connection";
import { daySeparator, fmtBeijing, fmtBeijingTime } from "../../utils/time";

const ROLE_LABEL: Record<string, string> = {
  manager: "管理 Agent · 需求 / 策略评估 / 审批",
  strategy: "策略子 Agent · 每日选股 / 买卖 / 日报",
};

const STATUS_META: Record<string, { color: string; text: string }> = {
  trial: { color: "blue", text: "试运行" },
  running: { color: "green", text: "运行中" },
  paused: { color: "orange", text: "手动暂停" },
  halted: { color: "red", text: "熔断暂停" },
  archived: { color: "default", text: "已归档" },
};

const AGENT_SORT = (role: string) => (role === "manager" ? 0 : 1);

export default function AgentsPage() {
  const navigate = useNavigate();
  const { token } = antTheme.useToken();
  const { message } = AntApp.useApp();
  const { subscribe } = useConnection();

  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [convs, setConvs] = useState<ConversationInfo[]>([]);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    try {
      const [{ agents: a }, { conversations: c }] = await Promise.all([
        listAgents(),
        listConversations(),
      ]);
      setAgents(a);
      setConvs(c);
    } catch (e) {
      message.error((e as Error).message ?? "加载失败");
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    const un = subscribe(() => {
      void reload();
    });
    return un;
  }, [subscribe, reload]);

  const convByAgent = useMemo(() => {
    const m = new Map<string, ConversationInfo>();
    convs
      .filter((c) => c.conv_type === "user_chat")
      .forEach((c) => m.set(c.agent_id, c));
    return m;
  }, [convs]);

  const busyAgentIds = useMemo(() => {
    const s = new Set<string>();
    convs.forEach((c) => {
      const l = c.last_message;
      if (l && l.direction === "user" && (l.status === "queued" || l.status === "processing")) {
        s.add(c.agent_id);
      }
    });
    return s;
  }, [convs]);

  const sorted = useMemo(() => {
    const order = (a: AgentInfo) =>
      a.status === "running" || a.status === "trial" ? 0 : 1;
    return [...agents].sort(
      (a, b) =>
        order(a) - order(b) ||
        AGENT_SORT(a.role) - AGENT_SORT(b.role) ||
        a.id.localeCompare(b.id),
    );
  }, [agents]);

  const enter = (agent: AgentInfo) => {
    navigate("/chat", { state: { agentId: agent.id } });
  };

  if (loading) {
    return (
      <div style={{ padding: 16 }}>
        <Skeleton active paragraph={{ rows: 6 }} />
      </div>
    );
  }

  return (
    <div style={{ padding: 16, minHeight: "100%" }}>
      <div style={{ marginBottom: 12 }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          Agent 看板
        </Typography.Title>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          账户/盈亏等交易维度待 spec-01 账户引擎接入后补齐；当前卡片展示生命周期状态与对话入口
        </Typography.Text>
      </div>

      {sorted.length === 0 ? (
        <Card>
          <Empty
            description="暂无 Agent——请先返回对话面板，管理 Agent 会在首次对话时就绪"
            style={{ padding: "48px 0" }}
          >
            <Button type="primary" icon={<MessageOutlined />} onClick={() => navigate("/chat")}>
              前往对话面板
            </Button>
          </Empty>
        </Card>
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
            gap: 12,
          }}
        >
          {sorted.map((agent) => {
            const conv = convByAgent.get(agent.id) ?? null;
            const last = conv?.last_message ?? null;
            const busy = conv ? busyAgentIds.has(conv.id) : false;
            const unread = conv?.unread ?? 0;
            const isManager = agent.role === "manager";
            const sm = STATUS_META[agent.status] ?? { color: "default", text: agent.status };
            return (
              <Card
                key={agent.id}
                hoverable
                styles={{ body: { padding: 14 } }}
                onClick={() => enter(agent)}
                style={{ borderColor: token.colorBorderSecondary }}
              >
                <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
                  <Badge count={unread} size="small" offset={[-4, 4]}>
                    <Avatar
                      size={44}
                      icon={isManager ? <CrownOutlined /> : <RobotOutlined />}
                      style={{ background: isManager ? "#722ed1" : "#13c2c2", flexShrink: 0 }}
                    />
                  </Badge>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      <Typography.Text strong style={{ fontSize: 15 }}>
                        {agent.name}
                      </Typography.Text>
                      <Tag color={sm.color} style={{ marginInlineEnd: 0 }}>
                        {sm.text}
                      </Tag>
                      {busy && (
                        <Tag color="processing" icon={<LoadingOutlined />} style={{ marginInlineEnd: 0 }}>
                          处理中
                        </Tag>
                      )}
                    </div>
                    <Typography.Text type="secondary" style={{ fontSize: 12, display: "block" }}>
                      {ROLE_LABEL[agent.role] ?? agent.role}
                    </Typography.Text>
                  </div>
                </div>

                <div
                  style={{
                    marginTop: 10,
                    padding: "8px 10px",
                    borderRadius: 6,
                    background: token.colorFillQuaternary,
                    fontSize: 12,
                  }}
                >
                  {last ? (
                    <>
                      <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                        {daySeparator(last.ts)} {fmtBeijingTime(last.ts)}
                      </Typography.Text>
                      <Typography.Text ellipsis style={{ display: "block", fontSize: 12 }}>
                        {last.direction === "user" ? "我：" : ""}
                        {(last.body || "").replace(/\s+/g, " ").slice(0, 60)}
                      </Typography.Text>
                    </>
                  ) : (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      尚未开始对话
                    </Typography.Text>
                  )}
                </div>

                <div
                  style={{
                    marginTop: 10,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 8,
                  }}
                >
                  <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                    创建于 {fmtBeijing(agent.created_ts)}
                  </Typography.Text>
                  <Tooltip title={conv ? "进入该 Agent 的对话" : "发送第一条消息以创建会话"}>
                    <Button
                      type="primary"
                      size="small"
                      ghost={!conv}
                      icon={<MessageOutlined />}
                      onClick={(e) => {
                        e.stopPropagation();
                        enter(agent);
                      }}
                    >
                      {conv ? "进入对话" : "开始对话"}
                    </Button>
                  </Tooltip>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}

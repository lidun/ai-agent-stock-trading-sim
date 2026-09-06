import { Avatar, Badge, Tag, Tooltip, Typography, theme as antTheme } from "antd";
import {
  RobotOutlined,
  WechatOutlined,
  CrownOutlined,
  MessageOutlined,
} from "@ant-design/icons";
import type { AgentInfo, ConversationInfo } from "../../api/endpoints";

/** 会话列表条目：某 Agent 的 user_chat 会话（无会话时显示“开始对话”） */
export interface ConvRow {
  agent: AgentInfo;
  conv: ConversationInfo | null;
}

export function ConversationList({
  rows,
  activeAgentId,
  busyAgents,
  onSelect,
}: {
  rows: ConvRow[];
  activeAgentId: string | null;
  busyAgents: ReadonlySet<string>;
  onSelect: (agentId: string) => void;
}) {
  const { token } = antTheme.useToken();
  const manager = rows.filter((r) => r.agent.role === "manager");
  const strategies = rows.filter((r) => r.agent.role !== "manager");

  const renderSection = (title: string, items: ConvRow[]) =>
    items.length > 0 ? (
      <div>
        <Typography.Text
          type="secondary"
          style={{ fontSize: 11, padding: "8px 12px 2px", display: "block" }}
        >
          {title}
        </Typography.Text>
        {items.map(({ agent, conv }) => {
          const active = agent.id === activeAgentId;
          const last = conv?.last_message;
          const preview =
            last &&
            `${last.direction === "user" ? "我：" : ""}${
              (last.body || "").replace(/\s+/g, " ").slice(0, 40)
            }`;
          const busy = conv ? busyAgents.has(conv.id) : false;
          return (
            <div
              key={agent.id}
              className="conv-row"
              onClick={() => onSelect(agent.id)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                padding: "9px 12px",
                cursor: "pointer",
                background: active ? token.colorPrimaryBg : "transparent",
                borderLeft: active ? `3px solid ${token.colorPrimary}` : "3px solid transparent",
              }}
            >
              <Badge count={conv?.unread ?? 0} size="small" offset={[-4, 4]}>
                <Avatar
                  size={36}
                  icon={agent.role === "manager" ? <CrownOutlined /> : <RobotOutlined />}
                  style={{
                    background: agent.role === "manager" ? "#722ed1" : "#13c2c2",
                    flexShrink: 0,
                  }}
                />
              </Badge>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <Typography.Text strong ellipsis style={{ fontSize: 13 }}>
                    {agent.name}
                  </Typography.Text>
                  {agent.role === "manager" && (
                    <Tag color="purple" style={{ fontSize: 10, marginInlineEnd: 0 }}>
                      M
                    </Tag>
                  )}
                </div>
                <Typography.Text
                  type="secondary"
                  ellipsis
                  style={{ fontSize: 12, display: "block" }}
                >
                  {conv ? (
                    <Tooltip title={preview}>
                      <span>{preview ?? "暂无消息"}</span>
                    </Tooltip>
                  ) : (
                    <span style={{ color: token.colorPrimary }}>
                      <MessageOutlined /> 开始对话
                    </span>
                  )}
                </Typography.Text>
              </div>
              {busy && (
                <Tag color="processing" style={{ fontSize: 10, marginInlineEnd: 0 }}>
                  处理中
                </Tag>
              )}
            </div>
          );
        })}
      </div>
    ) : null;

  return (
    <div style={{ overflowY: "auto", height: "100%" }}>
      <div style={{ padding: "12px 16px 4px" }}>
        <Typography.Text strong style={{ fontSize: 15 }}>
          <WechatOutlined /> 对话
        </Typography.Text>
      </div>
      {rows.length === 0 ? (
        <div style={{ padding: 24, textAlign: "center" }} className="muted">
          正在加载…
        </div>
      ) : (
        <>
          {renderSection("管理 Agent", manager)}
          {renderSection("策略 Agent", strategies)}
        </>
      )}
    </div>
  );
}

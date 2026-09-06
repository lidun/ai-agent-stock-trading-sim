import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import {
  App as AntApp,
  Avatar,
  Skeleton,
  Space,
  Spin,
  Tag,
  Typography,
  theme as antTheme,
} from "antd";
import { CrownOutlined, LoadingOutlined, RobotOutlined } from "@ant-design/icons";
import {
  listAgents,
  listConversations,
  listMessages,
  markConversationRead,
  openConversation,
  sendMessage,
  type AgentInfo,
  type ConversationInfo,
  type MessageInfo,
} from "../../api/endpoints";
import { useConnection, type ConvEvent } from "../../connection";
import { ConversationList, type ConvRow } from "./ConversationList";
import { MessageThread } from "./MessageThread";
import { Composer } from "./Composer";

const ROLE_LABEL: Record<string, string> = {
  manager: "管理 Agent（需求/策略评估/审批）",
  strategy: "策略子 Agent（每日选股/买卖/日报）",
};

const STATUS_LABEL: Record<string, string> = {
  trial: "试运行",
  running: "运行中",
  paused: "手动暂停",
  halted: "熔断暂停",
  archived: "已归档",
};

export default function ChatPage() {
  const { message } = AntApp.useApp();
  const { token } = antTheme.useToken();
  const { epoch, state: connState, subscribe } = useConnection();
  const location = useLocation();

  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [convs, setConvs] = useState<ConversationInfo[]>([]);
  const [activeAgentId, setActiveAgentId] = useState<string | null>(null);
  const [thread, setThread] = useState<{
    convId: string | null;
    messages: MessageInfo[];
    hasOlder: boolean;
    next_ts: string | null;
    next_id: string | null;
    loading: boolean;
    loadingOlder: boolean;
  }>({ convId: null, messages: [], hasOlder: false, next_ts: null, next_id: null, loading: false, loadingOlder: false });

  const bootedRef = useRef(false);
  // 记录已触发过已读回执的（会话→最新未读消息 id），避免重复上报 / 漏报新未读
  const markedUnread = useRef<Map<string, string>>(new Map());

  const convById = useMemo(() => {
    const m = new Map<string, ConversationInfo>();
    convs.forEach((c) => m.set(c.id, c));
    return m;
  }, [convs]);

  const agentById = useMemo(() => {
    const m = new Map<string, AgentInfo>();
    agents.forEach((a) => m.set(a.id, a));
    return m;
  }, [agents]);

  // 回执链进行中（由会话最近消息状态推导：user 消息仍 queued/processing）
  const busyConvIds = useMemo(() => {
    const set = new Set<string>();
    convs.forEach((c) => {
      const l = c.last_message;
      if (l && l.direction === "user" && (l.status === "queued" || l.status === "processing")) {
        set.add(c.id);
      }
    });
    return set;
  }, [convs]);

  const activeConvId = thread.convId;
  const busyNow = activeConvId ? busyConvIds.has(activeConvId) : false;

  const reloadConversations = useCallback(async () => {
    try {
      const [{ agents: a }, { conversations: c }] = await Promise.all([
        listAgents(),
        listConversations(),
      ]);
      setAgents(a);
      setConvs(c);
    } catch (e) {
      message.error((e as Error).message ?? "加载失败");
    }
  }, [message]);

  const loadThread = useCallback(
    async (convId: string, before?: { ts: string; id: string }) => {
      try {
        if (before) {
          const page = await listMessages(convId, before);
          setThread((t) =>
            t.convId === convId
              ? {
                  ...t,
                  messages: [...page.messages, ...t.messages],
                  hasOlder: page.has_older,
                  next_ts: page.next_before_ts,
                  next_id: page.next_before_id,
                  loadingOlder: false,
                }
              : t,
          );
        } else {
          const page = await listMessages(convId);
          setThread((t) =>
            t.convId === convId
              ? {
                  ...t,
                  messages: page.messages,
                  hasOlder: page.has_older,
                  next_ts: page.next_before_ts,
                  next_id: page.next_before_id,
                  loading: false,
                }
              : t,
          );
          const unread = page.messages.reduce<string | null>((acc, m) => {
            if (m.direction === "agent" && m.status === "delivered" && !m.read_ts) acc = m.id;
            return acc;
          }, null);
          if (unread && markedUnread.current.get(convId) !== unread) {
            markedUnread.current.set(convId, unread);
            void markConversationRead(convId).then(() => reloadConversations());
          }
        }
      } catch (e) {
        message.error((e as Error).message ?? "消息加载失败");
        setThread((t) => (t.convId === convId ? { ...t, loading: false, loadingOlder: false } : t));
      }
    },
    [message, reloadConversations],
  );

  const selectAgent = useCallback(
    async (agentId: string) => {
      setActiveAgentId(agentId);
      try {
        const conv = await openConversation(agentId);
        setThread({
          convId: conv.id,
          messages: [],
          hasOlder: false,
          next_ts: null,
          next_id: null,
          loading: true,
          loadingOlder: false,
        });
        await loadThread(conv.id);
        void reloadConversations();
      } catch (e) {
        message.error((e as Error).message ?? "会话打开失败");
      }
    },
    [loadThread, message, reloadConversations],
  );

  const handleConvEvent = useCallback(
    (ev: ConvEvent) => {
      void reloadConversations();
      if (activeConvIdRef.current === ev.conv_id) {
        if (ev.event === "message_status") {
          // 状态推进：更新对应本地消息，减少整表闪烁
          setThread((t) =>
            t.convId === ev.conv_id && ev.message
              ? {
                  ...t,
                  messages: t.messages.map((m) =>
                    m.id === ev.message?.id ? { ...m, status: ev.message.status as MessageInfo["status"] } : m,
                  ),
                }
              : t,
          );
        } else {
          void loadThread(ev.conv_id);
        }
      } else if (ev.event === "message_new" && ev.message?.status === "delivered") {
        // 其他会话新回复 → 依赖 reload 的未读角标
      }
    },
    [loadThread, reloadConversations],
  );

  // 记录当前会话 id，供事件回调引用（避免闭包过期）
  const activeConvIdRef = useRef<string | null>(null);
  useEffect(() => {
    activeConvIdRef.current = thread.convId;
  }, [thread.convId]);

  // 订阅站内通知事件（WS）
  useEffect(() => subscribe(handleConvEvent), [subscribe, handleConvEvent]);

  // 首次引导：加载 Agent + 会话，默认打开管理 Agent
  useEffect(() => {
    if (bootedRef.current) return;
    bootedRef.current = true;
    void (async () => {
      try {
        const [{ agents: a }, { conversations: c }] = await Promise.all([
          listAgents(),
          listConversations(),
        ]);
        setAgents(a);
        setConvs(c);
        // 优先跳转 Agent 看板等入口携带的 agentId（卡片点击 → 聚焦对应会话）
        const state = location.state as { agentId?: string } | null;
        const requested = state?.agentId ? a.find((x) => x.id === state.agentId) : undefined;
        const target = requested ?? a.find((x) => x.role === "manager");
        if (target) await selectAgent(target.id);
      } catch (e) {
        message.error((e as Error).message ?? "初始化失败");
      }
    })();
  }, [message, selectAgent, location.state]);

  // 断线重连后 refetch（spec-06 §3）
  const prevEpoch = useRef(0);
  useEffect(() => {
    if (prevEpoch.current !== 0 && epoch > prevEpoch.current) {
      void reloadConversations();
      if (activeConvIdRef.current) void loadThread(activeConvIdRef.current);
    }
    prevEpoch.current = epoch;
  }, [epoch, reloadConversations, loadThread]);

  const rows: ConvRow[] = useMemo(() => {
    const seen = new Set<string>();
    const out: ConvRow[] = [];
    const order = (role: string) => (role === "manager" ? 0 : 1);
    const items = [...agents].sort((x, y) => order(x.role) - order(y.role) || x.id.localeCompare(y.id));
    items.forEach((agent) => {
      const conv = convs.find((c) => c.agent_id === agent.id && c.conv_type === "user_chat") ?? null;
      seen.add(agent.id);
      out.push({ agent, conv });
    });
    convs
      .filter((c) => !seen.has(c.agent_id) && c.conv_type === "user_chat")
      .forEach((c) => {
        const agent = agentById.get(c.agent_id);
        if (agent) out.push({ agent, conv: c });
      });
    return out;
  }, [agents, convs, agentById]);

  const onSend = useCallback(
    async (text: string): Promise<boolean> => {
      if (!activeAgentId) return false;
      try {
        const conv =
          activeConvIdRef.current && convById.has(activeConvIdRef.current)
            ? activeConvIdRef.current
            : (await openConversation(activeAgentId)).id;
        const { message: sent } = await sendMessage(conv, text);
        if (conv !== activeConvIdRef.current) {
          setThread({
            convId: conv,
            messages: [sent],
            hasOlder: false,
            next_ts: null,
            next_id: null,
            loading: false,
            loadingOlder: false,
          });
        } else {
          setThread((t) => (t.convId === conv ? { ...t, messages: [...t.messages, sent] } : t));
        }
        void reloadConversations();
        return true;
      } catch (e) {
        message.error((e as Error).message ?? "发送失败");
        return false;
      }
    },
    [activeAgentId, convById, message, reloadConversations],
  );

  const activeAgent = activeAgentId ? agentById.get(activeAgentId) : null;

  return (
    <div
      style={{
        height: "calc(100vh - 94px)",
        minHeight: 480,
        display: "flex",
        background: token.colorBgLayout,
      }}
    >
      {/* 联系人式会话列表 */}
      <div
        style={{
          width: 292,
          borderRight: `1px solid ${token.colorBorderSecondary}`,
          background: token.colorBgContainer,
          flexShrink: 0,
          display: "flex",
          flexDirection: "column",
        }}
      >
        <ConversationList
          rows={rows}
          activeAgentId={activeAgentId}
          busyAgents={busyConvIds}
          onSelect={(id) => void selectAgent(id)}
        />
      </div>

      {/* 会话区 */}
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        {activeAgent ? (
          <>
            <div
              style={{
                height: 52,
                padding: "6px 16px",
                borderBottom: `1px solid ${token.colorBorderSecondary}`,
                background: token.colorBgContainer,
                display: "flex",
                alignItems: "center",
                gap: 10,
              }}
            >
              <Avatar
                size={32}
                icon={activeAgent.role === "manager" ? <CrownOutlined /> : <RobotOutlined />}
                style={{ background: activeAgent.role === "manager" ? "#722ed1" : "#13c2c2" }}
              />
              <div style={{ minWidth: 0 }}>
                <Space size={6}>
                  <Typography.Text strong>{activeAgent.name}</Typography.Text>
                  <Tag color={activeAgent.status === "running" ? "green" : "orange"}>
                    {STATUS_LABEL[activeAgent.status] ?? activeAgent.status}
                  </Tag>
                </Space>
                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                    {ROLE_LABEL[activeAgent.role]}
                  </Typography.Text>
                </div>
              </div>
              {busyNow && (
                <Tag
                  icon={<LoadingOutlined />}
                  color="processing"
                  style={{ marginInlineStart: "auto" }}
                >
                  处理中（排队 → 处理中 → 已回复）
                </Tag>
              )}
              {connState !== "online" && (
                <Tag color="error" style={{ marginInlineStart: "auto" }}>
                  连接中断，自动重连中
                </Tag>
              )}
            </div>

            <div style={{ flex: 1, minHeight: 0 }}>
              {thread.loading ? (
                <div style={{ padding: 24 }}>
                  <Skeleton active paragraph={{ rows: 6 }} />
                </div>
              ) : activeConvId && thread.convId === activeConvId ? (
                <MessageThread
                  convId={activeConvId}
                  messages={thread.messages}
                  hasOlder={thread.hasOlder}
                  loading={thread.loadingOlder}
                  onLoadOlder={() => {
                    if (thread.next_ts && thread.next_id) {
                      setThread((t) => ({ ...t, loadingOlder: true }));
                      void loadThread(activeConvId, { ts: thread.next_ts, id: thread.next_id });
                    }
                  }}
                />
              ) : (
                <div
                  style={{
                    height: "100%",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  <Space direction="vertical" align="center">
                    <Spin size="large" />
                    <Typography.Text type="secondary">正在打开会话…</Typography.Text>
                  </Space>
                </div>
              )}
            </div>

            <Composer disabled={!activeAgentId} onSend={onSend} />
          </>
        ) : (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
            <Skeleton active paragraph={{ rows: 8 }} style={{ width: 480 }} />
          </div>
        )}
      </div>
    </div>
  );
}

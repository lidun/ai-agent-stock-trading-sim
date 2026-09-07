import { useEffect, useLayoutEffect, useRef } from "react";
import { Button, Spin, Typography, theme as antTheme } from "antd";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { RobotOutlined, UserOutlined, UpOutlined } from "@ant-design/icons";
import type { MessageInfo } from "../../api/endpoints";
import { daySeparator, fmtBeijingTime, isSameBeijingDay } from "../../utils/time";

const AGENT_TYPE_LABEL: Record<string, string> = {
  reply: "提问回复",
  daily_report: "日报",
  report: "日报直达",
  abnormal_report: "异常上报",
  approval_receipt: "审批回执",
  daily_summary: "总汇报",
  system_event: "系统事件",
};

const STATUS_LABEL: Record<string, { text: string; color?: string }> = {
  queued: { text: "排队中…" },
  processing: { text: "处理中…" },
  delivered: { text: "已送达" },
  pending_review: { text: "已转管理 Agent 审阅" },
  failed: { text: "发送失败", color: "#cf1322" },
};

function Bubble({ message }: { message: MessageInfo }) {
  const { token } = antTheme.useToken();
  const isUser = message.direction === "user";
  const status = STATUS_LABEL[message.status] ?? { text: message.status };
  const showStatus = isUser && message.status !== "delivered";
  return (
    <div id={`msg-${message.id}`} style={{ display: "flex", justifyContent: isUser ? "flex-end" : "flex-start" }}>
      {!isUser && (
        <div
          style={{
            width: 28,
            height: 28,
            borderRadius: "50%",
            background: "#722ed1",
            color: "#fff",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            marginRight: 8,
            flexShrink: 0,
            marginTop: 4,
          }}
        >
          <RobotOutlined style={{ fontSize: 14 }} />
        </div>
      )}
      <div style={{ maxWidth: "72%" }}>
        {!isUser && (
          <div style={{ fontSize: 11, color: "#722ed1", marginBottom: 2, paddingLeft: 2 }}>
            {AGENT_TYPE_LABEL[message.msg_type] ?? message.msg_type}
          </div>
        )}
        <div
          style={{
            padding: "8px 12px",
            borderRadius: isUser ? "12px 12px 2px 12px" : "12px 12px 12px 2px",
            background: isUser ? "#1677ff" : token.colorBgContainer,
            border: isUser ? "none" : `1px solid ${token.colorBorderSecondary}`,
            color: isUser ? "#fff" : token.colorText,
            fontSize: 14,
            lineHeight: 1.65,
            overflowWrap: "break-word",
          }}
        >
          {isUser ? (
            <span style={{ whiteSpace: "pre-wrap" }}>{message.body}</span>
          ) : (
            <div className="md-body">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.body}</ReactMarkdown>
            </div>
          )}
        </div>
        <div
          style={{
            marginTop: 2,
            fontSize: 11,
            color: status.color ?? token.colorTextTertiary,
            textAlign: isUser ? "right" : "left",
            paddingInline: 2,
          }}
        >
          <span className="zone-bj">{fmtBeijingTime(message.ts)}</span>
          {showStatus && ` · ${status.text}`}
        </div>
      </div>
      {isUser && (
        <div
          style={{
            width: 26,
            height: 26,
            borderRadius: "50%",
            background: "#1677ff",
            color: "#fff",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            marginLeft: 8,
            flexShrink: 0,
            marginTop: 4,
          }}
        >
          <UserOutlined style={{ fontSize: 13 }} />
        </div>
      )}
    </div>
  );
}

export function MessageThread({
  convId,
  messages,
  hasOlder,
  loading,
  onLoadOlder,
}: {
  convId: string;
  messages: MessageInfo[];
  hasOlder: boolean;
  loading: boolean;
  onLoadOlder: () => void;
}) {
  const { token } = antTheme.useToken();
  const scrollRef = useRef<HTMLDivElement>(null);
  const lastId = useRef<string | null>(null);
  // 加载更早消息前记录滚动位置，补插完成后保持视口不跳动
  const pinRef = useRef<{ scrollTop: number; scrollHeight: number } | null>(null);
  const olderLoadingRef = useRef(false);

  useLayoutEffect(() => {
    if (!olderLoadingRef.current || loading || messages.length === 0) return;
    olderLoadingRef.current = false;
    const el = scrollRef.current;
    const pin = pinRef.current;
    pinRef.current = null;
    if (!el || !pin) return;
    el.scrollTop = pin.scrollTop + (el.scrollHeight - pin.scrollHeight);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, loading]);

  const loadOlderPinned = () => {
    const el = scrollRef.current;
    if (el) pinRef.current = { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight };
    olderLoadingRef.current = true;
    onLoadOlder();
  };

  useEffect(() => {
    lastId.current = null;
    pinRef.current = null;
    olderLoadingRef.current = false;
  }, [convId]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || messages.length === 0) return;
    const newest = messages[messages.length - 1].id;
    if (newest !== lastId.current) {
      lastId.current = newest;
      el.scrollTo({ top: el.scrollHeight });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages]);

  // 打开会话时定位到首条未读（spec-06 §6.1），仅当整段历史已加载完才可靠
  const anchored = useRef<string | null>(null);
  useEffect(() => {
    if (hasOlder || loading) return;
    const idx = messages.findIndex(
      (m) => m.direction === "agent" && m.status === "delivered" && !m.read_ts,
    );
    if (idx < 0) return;
    if (anchored.current === convId) return;
    anchored.current = convId;
    const target = document.getElementById(`msg-${messages[idx].id}`);
    target?.scrollIntoView({ block: "start" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [convId, messages, hasOlder, loading]);

  const nodes: React.ReactNode[] = [];
  let prev: MessageInfo | null = null;
  messages.forEach((m) => {
    if (!prev || !isSameBeijingDay(prev.ts, m.ts)) {
      nodes.push(
        <div key={`sep-${m.ts}`} style={{ textAlign: "center", margin: "14px 0 8px" }}>
          <span
            className="muted"
            style={{ fontSize: 11, background: token.colorFillTertiary, padding: "1px 10px", borderRadius: 999 }}
          >
            {daySeparator(m.ts)}
          </span>
        </div>,
      );
    }
    nodes.push(<Bubble key={m.id} message={m} />);
    prev = m;
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div
        ref={scrollRef}
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "8px 16px 12px",
          display: "flex",
          flexDirection: "column",
          gap: 6,
        }}
      >
        {hasOlder && (
          <div style={{ textAlign: "center", padding: 6 }}>
            <Button
              size="small"
              type="text"
              icon={<UpOutlined />}
              loading={loading && messages.length > 0}
              disabled={loading}
              onClick={loadOlderPinned}
            >
              加载更早消息
            </Button>
          </div>
        )}
        {loading && messages.length === 0 && (
          <div style={{ textAlign: "center", padding: 48 }}>
            <Spin />
          </div>
        )}
        {!loading && messages.length === 0 && (
          <div style={{ textAlign: "center", padding: 48 }} className="muted">
            <Typography.Text type="secondary">
              尚未开始对话——发送第一条消息，验证「回执链 + 实时回复」链路
            </Typography.Text>
          </div>
        )}
        {nodes}
      </div>
    </div>
  );
}

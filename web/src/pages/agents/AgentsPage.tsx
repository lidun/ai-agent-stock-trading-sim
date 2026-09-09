import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  App as AntApp,
  Avatar,
  Badge,
  Button,
  Card,
  Dropdown,
  Empty,
  Input,
  Modal,
  Skeleton,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  theme as antTheme,
} from "antd";
import {
  CaretRightOutlined,
  ControlOutlined,
  ClearOutlined,
  CrownOutlined,
  ExperimentOutlined,
  LockOutlined,
  LoadingOutlined,
  MessageOutlined,
  PauseCircleOutlined,
  ProfileOutlined,
  RobotOutlined,
  StopOutlined,
} from "@ant-design/icons";
import {
  batchControl,
  batchSellAll,
  controlAgent,
  emergencySellAll,
  freezeSecurity,
  listAccounts,
  listAgents,
  listConversations,
  listFrozen,
  unfreezeSecurity,
  type AccountInfo,
  type AgentInfo,
  type ConversationInfo,
  type ControlOp,
  type FrozenSecurity,
} from "../../api/endpoints";
import { useConnection } from "../../connection";
import { daySeparator, fmtBeijing, fmtBeijingTime } from "../../utils/time";
import AgentOverviewDrawer from "./AgentOverviewDrawer";
import TrialAcceptance from "./TrialAcceptance";

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

const GRAN_LABEL: Record<string, string> = {
  eod_replay: "收盘回放撮合(EOD)",
  intraday_5m: "盘中撮合(5m)",
  intraday_1m: "盘中撮合(1m)",
};

/** 盈亏着色：A 股红涨绿跌（spec-06 §5.2，涨红跌绿全局口径） */
function pnlColor(v: number): string {
  if (v > 0) return "#cf1322";
  if (v < 0) return "#389e0d";
  return "inherit";
}

function money(v: string): string {
  return Number(v).toLocaleString("zh-CN", {
    style: "currency",
    currency: "CNY",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function Metric({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 11, color: "inherit", opacity: 0.55 }}>{label}</div>
      <div
        style={{
          fontSize: 13,
          fontWeight: 600,
          color,
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {value}
      </div>
    </div>
  );
}

export default function AgentsPage() {
  const navigate = useNavigate();
  const { token } = antTheme.useToken();
  const { message, modal } = AntApp.useApp();
  const { subscribe } = useConnection();

  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [convs, setConvs] = useState<ConversationInfo[]>([]);
  const [accounts, setAccounts] = useState<AccountInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [trialAgent, setTrialAgent] = useState<AgentInfo | null>(null);
  const [overviewAgent, setOverviewAgent] = useState<AgentInfo | null>(null);

  const reload = useCallback(async () => {
    try {
      const [{ agents: a }, { conversations: c }, { accounts: ac }] = await Promise.all([
        listAgents(),
        listConversations(),
        listAccounts(),
      ]);
      setAgents(a);
      setConvs(c);
      setAccounts(ac);
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

  const accountByAgent = useMemo(() => {
    const m = new Map<string, AccountInfo>();
    accounts
      .filter((a) => a.role === "main")
      .forEach((a) => m.set(a.agent_id, a));
    return m;
  }, [accounts]);

  const [clearAgent, setClearAgent] = useState<AgentInfo | null>(null);
  const [clearConfirm, setClearConfirm] = useState("");
  const [clearing, setClearing] = useState(false);
  const [batchClearOpen, setBatchClearOpen] = useState(false);
  const [batchClearConfirm, setBatchClearConfirm] = useState("");
  const [batchClearing, setBatchClearing] = useState(false);

  const batchRun = (op: ControlOp) => {
    const meta = {
      pause_buy: {
        title: "全局冻结买入？",
        text: "将冻结全部运行中策略 Agent 的买入（保留卖出与风控）；非运行/已是目标态的 Agent 自动跳过。",
      },
      halt: {
        title: "全局熔断冻结？",
        text: "将对全部运行中策略 Agent 买卖全停（保留结算与风控）；非运行 Agent 自动跳过。",
      },
      resume: {
        title: "全局解除冻结？",
        text: "将恢复全部处于冻结态（paused_buy/halted）的策略 Agent 主账户为 normal。",
      },
    }[op];
    modal.confirm({
      title: meta.title,
      content: <Typography.Text type="secondary">{meta.text}</Typography.Text>,
      okText: "确认",
      okButtonProps: { type: op === "resume" ? "primary" : "default", danger: op !== "resume" },
      cancelText: "取消",
      onOk: async () => {
        try {
          const r = await batchControl(op);
          message.success(`全局直控完成：生效 ${r.applied_count} 个，跳过 ${r.skipped_count} 个`);
          void reload();
        } catch (e) {
          message.error((e as Error).message ?? "全局直控失败");
        }
      },
    });
  };

  const doBatchClear = async () => {
    if (batchClearConfirm.trim() !== "清仓") return;
    setBatchClearing(true);
    try {
      const r = await batchSellAll();
      message.success(
        r.total_holdings === 0
          ? "全部运行中策略 Agent 均无持仓"
          : `已为 ${r.agents_count} 个 Agent 生成 ${r.total_orders} 张卖出条件单（持仓 ${r.total_holdings} 只）`,
      );
      setBatchClearOpen(false);
      setBatchClearConfirm("");
      void reload();
    } catch (e) {
      message.error((e as Error).message ?? "全局清仓失败");
    } finally {
      setBatchClearing(false);
    }
  };

  const [frozenAgent, setFrozenAgent] = useState<AgentInfo | null>(null);
  const [frozenRows, setFrozenRows] = useState<FrozenSecurity[]>([]);
  const [frozenLoading, setFrozenLoading] = useState(false);
  const [frozenSymbol, setFrozenSymbol] = useState("");
  const [frozenReason, setFrozenReason] = useState("");

  const reloadFrozen = useCallback(async (agentId: string) => {
    setFrozenLoading(true);
    try {
      const r = await listFrozen(agentId);
      setFrozenRows(r.frozen);
    } catch (e) {
      message.error((e as Error).message ?? "加载冻结清单失败");
    } finally {
      setFrozenLoading(false);
    }
  }, [message]);

  const openFrozen = (agent: AgentInfo) => {
    setFrozenSymbol("");
    setFrozenReason("");
    setFrozenAgent(agent);
    void reloadFrozen(agent.id);
  };

  const doFreeze = async () => {
    const sym = frozenSymbol.trim();
    if (!frozenAgent || !sym) {
      message.warning("请填写要冻结的证券代码");
      return;
    }
    try {
      const r = await freezeSecurity(frozenAgent.id, sym, frozenReason.trim());
      message.success(`已冻结买入 ${sym}${r.cancelled_buy_orders > 0 ? `，同步取消该票买入单 ${r.cancelled_buy_orders} 张` : ""}`);
      setFrozenSymbol("");
      setFrozenReason("");
      void reloadFrozen(frozenAgent.id);
      void reload();
    } catch (e) {
      message.error((e as Error).message ?? "冻结失败");
    }
  };

  const doUnfreeze = (row: FrozenSecurity) => {
    if (!frozenAgent) return;
    modal.confirm({
      title: `解除冻结 ${row.symbol}？`,
      content: "解除后该 Agent 恢复买入此证券；此前因冻结被取消的单据不会自动重建。",
      okText: "解除冻结",
      okButtonProps: { type: "primary" },
      onOk: async () => {
        try {
          await unfreezeSecurity(frozenAgent.id, row.symbol);
          message.success(`已解除冻结 ${row.symbol}`);
          void reloadFrozen(frozenAgent.id);
        } catch (e) {
          message.error((e as Error).message ?? "解除失败");
        }
      },
    });
  };

  const runControl = (agent: AgentInfo, op: ControlOp) => {
    const meta = {
      pause_buy: {
        title: "冻结买入？",
        text: "冻结后该 Agent 的买入类订单即时被引擎拦截（秒级生效）；卖出与风控单保留可继续执行；解除冻结随时可恢复。",
        ok: "确认冻结",
      },
      halt: {
        title: "熔断冻结该 Agent？",
        text: "熔断后买卖全停、任何订单即时被引擎拦截（秒级生效）；保留结算与风控评估。",
        ok: "确认熔断",
      },
      resume: {
        title: "解除冻结并恢复？",
        text: "账户将恢复 normal 状态，买入/卖出信号恢复可下单。",
        ok: "恢复运行",
      },
    }[op];
    modal.confirm({
      title: meta.title,
      content: <Typography.Text type="secondary">{meta.text}</Typography.Text>,
      okText: meta.ok,
      okButtonProps: { type: op === "resume" ? "primary" : "default", danger: op !== "resume" },
      cancelText: "取消",
      onOk: async () => {
        try {
          await controlAgent(agent.id, op);
          message.success(op === "resume" ? "已解除冻结并恢复" : "已即时生效（引擎闸门秒级拦截）");
          void reload();
        } catch (e) {
          message.error((e as Error).message ?? "直控操作失败");
        }
      },
    });
  };

  const doEmergencyClear = async () => {
    if (!clearAgent || clearConfirm.trim() !== "清仓") return;
    setClearing(true);
    try {
      const r = await emergencySellAll(clearAgent.id);
      if (r.blocked_halted) {
        message.warning("账户处于熔断冻结态，卖出被闸门拦截——请先解除熔断再清仓");
      } else {
        message.success(r.holdings === 0 ? "当前无持仓，无需清仓" : `已生成卖出条件单 ${r.orders.length} 张`);
      }
      setClearAgent(null);
      setClearConfirm("");
      void reload();
    } catch (e) {
      message.error((e as Error).message ?? "紧急清仓失败");
    } finally {
      setClearing(false);
    }
  };

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
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            Agent 看板
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            模拟账户/净值/盈亏已接入（spec-01 份额法口径）；持仓与当日结算待撮合引擎落地后更新
          </Typography.Text>
        </div>
        <Dropdown
          trigger={["click"]}
          menu={{
            items: [
              {
                key: "pause_buy", icon: <PauseCircleOutlined />,
                label: "全部冻结买入（保留卖出与风控）",
              },
              { key: "halt", icon: <StopOutlined />, label: "全部熔断冻结（买卖全停）" },
              { key: "resume", icon: <CaretRightOutlined />, label: "全部解除冻结 / 恢复" },
              { type: "divider" as const },
              {
                key: "clear", icon: <ClearOutlined />, danger: true,
                label: "全部紧急清仓",
              },
            ],
            onClick: ({ key }) => {
              if (key === "clear") {
                setBatchClearConfirm("");
                setBatchClearOpen(true);
                return;
              }
              batchRun(key as ControlOp);
            },
          }}
        >
          <Button icon={<ControlOutlined />}>全局直控</Button>
        </Dropdown>
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
            const acc = accountByAgent.get(agent.id) ?? null;
            const frozen = acc?.status === "paused_buy" || acc?.status === "halted";
            const controlMenu =
              agent.status === "running" && !isManager
                ? [
                    ...(frozen
                      ? [{
                          key: "resume", icon: <CaretRightOutlined />,
                          label: "解除冻结 / 恢复",
                        }]
                      : [
                          {
                            key: "pause_buy", icon: <PauseCircleOutlined />,
                            label: "冻结买入（保留卖出与风控）",
                          },
                          {
                            key: "halt", icon: <StopOutlined />,
                            label: "熔断冻结（买卖全停）",
                          },
                        ]),
                    {
                      key: "frozen", icon: <LockOutlined />,
                      label: "冻结证券…（逐票清单）",
                    },
                    { type: "divider" as const },
                    {
                      key: "clear", icon: <ClearOutlined />, danger: true,
                      label: "紧急清仓",
                    },
                  ]
                : null;
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
                      {frozen && (
                        <Tag
                          color={acc?.status === "paused_buy" ? "orange" : "red"}
                          style={{ marginInlineEnd: 0 }}
                        >
                          {acc?.status === "paused_buy" ? "冻结买入（保留卖出）" : "熔断冻结"}
                        </Tag>
                      )}
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

                {acc ? (
                  <div
                    style={{
                      marginTop: 10,
                      display: "grid",
                      gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
                      gap: "6px 12px",
                    }}
                  >
                    <Metric label="当前现金" value={money(acc.cash)} />
                    <Metric label="份额净值" value={Number(acc.nav).toFixed(4)} />
                    <Metric label="累计盈亏" value={money(acc.total_pnl)} color={pnlColor(Number(acc.total_pnl))} />
                    <Metric label="今日盈亏" value={money(acc.today_pnl)} color={pnlColor(Number(acc.today_pnl))} />
                  </div>
                ) : (
                  <div
                    style={{
                      marginTop: 10,
                      padding: "8px 10px",
                      borderRadius: 6,
                      background: token.colorFillQuaternary,
                      fontSize: 12,
                    }}
                  >
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      非交易账户 · 承担需求评估/审批等管理职责，不参与撮合与资金核算
                    </Typography.Text>
                  </div>
                )}

                <div
                  style={{
                    marginTop: 10,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 8,
                  }}
                >
                  <div style={{ minWidth: 0 }}>
                    <Typography.Text type="secondary" style={{ fontSize: 11, display: "block" }}>
                      {acc ? GRAN_LABEL[acc.granularity] ?? acc.granularity : "管理角色"}
                    </Typography.Text>
                    <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                      创建于 {fmtBeijing(agent.created_ts)}
                    </Typography.Text>
                  </div>
                  <Space size={6}>
                    <Tooltip title="概览：章程版本链 + 已下发能力包聚合（spec-05 §4.1/§5 只读）">
                      <Button
                        size="small"
                        icon={<ProfileOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          setOverviewAgent(agent);
                        }}
                      >
                        概览
                      </Button>
                    </Tooltip>
                    {controlMenu && (
                      <Dropdown
                        trigger={["click"]}
                        menu={{
                          items: controlMenu,
                          onClick: ({ key }) => {
                            if (key === "clear") {
                              setClearConfirm("");
                              setClearAgent(agent);
                              return;
                            }
                            if (key === "frozen") {
                              openFrozen(agent);
                              return;
                            }
                            runControl(agent, key as ControlOp);
                          },
                        }}
                      >
                        <Button size="small" icon={<ControlOutlined />} onClick={(e) => e.stopPropagation()}>
                          直控
                        </Button>
                      </Dropdown>
                    )}
                    {agent.status === "trial" && (
                      <Tooltip title="试运行验收：门槛预览 + 上线/否决决策留证（spec-05 §6）">
                        <Button
                          size="small"
                          icon={<ExperimentOutlined />}
                          onClick={(e) => {
                            e.stopPropagation();
                            setTrialAgent(agent);
                          }}
                        >
                          验收
                        </Button>
                      </Tooltip>
                    )}
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
                  </Space>
                </div>
              </Card>
            );
          })}
        </div>
      )}

      {trialAgent && (
        <TrialAcceptance
          agent={trialAgent}
          open={trialAgent !== null}
          onClose={() => setTrialAgent(null)}
          onDone={() => {
            setTrialAgent(null);
            void reload();
          }}
        />
      )}

      <AgentOverviewDrawer agent={overviewAgent} onClose={() => setOverviewAgent(null)} />

      <Modal
        title={`冻结证券 · ${frozenAgent?.name ?? ""}（冻结买入保留卖出）`}
        open={frozenAgent !== null}
        onCancel={() => setFrozenAgent(null)}
        footer={null}
        width={520}
      >
        <Space.Compact style={{ display: "flex", marginBottom: 8 }}>
          <Input
            placeholder="证券代码，如 600519"
            value={frozenSymbol}
            onChange={(e) => setFrozenSymbol(e.target.value)}
            maxLength={16}
          />
          <Input
            placeholder="冻结原因（可选）"
            value={frozenReason}
            onChange={(e) => setFrozenReason(e.target.value)}
            maxLength={200}
          />
          <Button type="primary" icon={<LockOutlined />} onClick={() => void doFreeze()}>
            冻结
          </Button>
        </Space.Compact>
        <Table<FrozenSecurity>
          rowKey="id"
          size="small"
          loading={frozenLoading}
          dataSource={frozenRows}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前无冻结证券" /> }}
          pagination={false}
          columns={[
            { title: "证券", dataIndex: "symbol", width: 110 },
            { title: "冻结原因", dataIndex: "reason", ellipsis: true },
            { title: "冻结时间", dataIndex: "created_ts", width: 160, render: (v: string) => fmtBeijingTime(v) },
            {
              title: "操作", key: "op", width: 90,
              render: (_, row) => (
                <Button size="small" onClick={() => doUnfreeze(row)}>
                  解除
                </Button>
              ),
            },
          ]}
        />
      </Modal>

      <Modal
        title="紧急清仓（需输入确认）"
        open={clearAgent !== null}
        onCancel={() => {
          setClearAgent(null);
          setClearConfirm("");
        }}
        onOk={() => void doEmergencyClear()}
        confirmLoading={clearing}
        okText="确认清仓"
        okButtonProps={{ danger: true, disabled: clearConfirm.trim() !== "清仓" }}
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
          将为该 Agent 主账户全部持仓逐票生成市价卖出条件单，不经 LLM、即时生效。
          熔断冻结期间卖出同样被闸门拦截（需先解除）。
        </Typography.Paragraph>
        <Input
          placeholder="请输入“清仓”以确认本次高危操作"
          value={clearConfirm}
          onChange={(e) => setClearConfirm(e.target.value)}
          maxLength={16}
        />
      </Modal>

      <Modal
        title="全局紧急清仓（需输入确认）"
        open={batchClearOpen}
        onCancel={() => {
          setBatchClearOpen(false);
          setBatchClearConfirm("");
        }}
        onOk={() => void doBatchClear()}
        confirmLoading={batchClearing}
        okText="确认全局清仓"
        okButtonProps={{ danger: true, disabled: batchClearConfirm.trim() !== "清仓" }}
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
          将为全部运行中策略 Agent 的主账户逐票生成市价卖出条件单，不经 LLM、即时生效。
          熔断冻结中的 Agent 卖出会被闸门拦截并在结果中提示。
        </Typography.Paragraph>
        <Input
          placeholder="请输入“清仓”以确认本次高危操作"
          value={batchClearConfirm}
          onChange={(e) => setBatchClearConfirm(e.target.value)}
          maxLength={16}
        />
      </Modal>
    </div>
  );
}

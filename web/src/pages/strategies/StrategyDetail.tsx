import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Empty,
  Flex,
  Popover,
  Select,
  Skeleton,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  ArrowRightOutlined,
  CaretRightOutlined,
  ClearOutlined,
  LinkOutlined,
  LockOutlined,
  PauseCircleOutlined,
  StopOutlined,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import {
  controlAgent,
  fetchEquityCurve,
  fetchEvolution,
  fetchExitTrackings,
  fetchReportVersions,
  fetchStrategyMetrics,
  fetchStrategyMemory,
  fetchStrategyProfile,
  fetchStrategyVersions,
  fetchValidationWindows,
  getAccount,
  listAccountConditionOrders,
  listAccountHoldings,
  listAccountTrades,
  listAgents,
  listFrozen,
  listReportTimeline,
  unfreezeSecurity,
  type AccountInfo,
  type AgentInfo,
  type ConditionOrderInfo,
  type ControlOp,
  type CurveRange,
  type EquityCurve,
  type ExitTrackingList,
  type FrozenSecurity,
  type HoldingInfo,
  type ReportTimelineEntry,
  type StrategyEvolution,
  type StrategyMetrics,
  type StrategyProfile,
  type StrategyMemoryList,
  type StrategyVersionList,
  type ValidationWindowList,
  type TradeInfo,
} from "../../api/endpoints";
import { fmtBeijingTime } from "../../utils/time";
import MetricSummaryCard from "./MetricSummaryCard";
import EquityCurveCard from "./EquityCurveCard";
import StrategyEvolutionCard from "./StrategyEvolutionCard";
import StrategyProfileCard from "./StrategyProfileCard";
import StrategyVersionsCard from "./StrategyVersionsCard";
import ValidationWindowsCard from "./ValidationWindowsCard";
import SellTrackingCard from "./SellTrackingCard";

const OT_LABEL: Record<string, string> = {
  buy: "买入",
  sell_take_profit: "止盈卖出",
  sell_stop: "止损卖出",
  sell_trail: "移动止损卖出",
  sell_open_board: "开盘卖出",
  buy_seal_confirm: "扫板买入",
  basket: "篮子",
  time: "定时",
  combo: "组合",
};

const CO_STATUS: Record<string, { color: string; text: string }> = {
  active: { color: "blue", text: "生效中" },
  partial: { color: "processing", text: "部分成交" },
  filled: { color: "green", text: "已成交" },
  cancelled: { color: "default", text: "已撤单" },
  expired: { color: "orange", text: "已过期" },
  invalid: { color: "red", text: "无效" },
};

function money(v: string): string {
  return Number(v || "0").toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function price(v: string): string {
  return Number(v || "0").toLocaleString("zh-CN", { minimumFractionDigits: 3, maximumFractionDigits: 3 });
}
function qty(v: string): string {
  return Number(v || "0").toLocaleString("zh-CN", { maximumFractionDigits: 4 });
}
/** A 股习惯：涨红跌绿（spec-06 §5.1） */
function pnlColor(v: string): string {
  const n = Number(v || "0");
  if (n > 0) return "#cf1322";
  if (n < 0) return "#389e0d";
  return "rgba(0,0,0,0.88)";
}
function signed(v: string): string {
  const n = Number(v || "0");
  return `${n > 0 ? "+" : ""}${money(v)}`;
}

const EMPTY_NOTE = "撮合引擎尚未写入数据——EOD 回放结算落地后，持仓/条件单将在策略详情呈现（spec-01 §2.2/§2.3）";

export default function StrategyDetailPage() {
  const { message, modal } = AntApp.useApp();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();

  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [account, setAccount] = useState<AccountInfo | null>(null);
  const [holdings, setHoldings] = useState<HoldingInfo[]>([]);
  const [orders, setOrders] = useState<ConditionOrderInfo[]>([]);
  const [trades, setTrades] = useState<TradeInfo[]>([]);
  const [timeline, setTimeline] = useState<ReportTimelineEntry[]>([]);
  const [frozen, setFrozen] = useState<FrozenSecurity[]>([]);
  const [loading, setLoading] = useState(true);
  const [unsettledDates, setUnsettledDates] = useState<string[]>([]);
  const [metrics, setMetrics] = useState<StrategyMetrics | null>(null);
  const [curve, setCurve] = useState<EquityCurve | null>(null);
  const [evolution, setEvolution] = useState<StrategyEvolution | null>(null);
  const [profile, setProfile] = useState<StrategyProfile | null>(null);
  const [exitTracks, setExitTracks] = useState<ExitTrackingList | null>(null);
  const [memory, setMemory] = useState<StrategyMemoryList | null>(null);
  const [versions, setVersions] = useState<StrategyVersionList | null>(null);
  const [windows, setWindows] = useState<ValidationWindowList | null>(null);
  const [range, setRange] = useState<CurveRange>("all");
  const [p2Loading, setP2Loading] = useState(true);
  const [reportScope, setReportScope] = useState<string>("main");
  const [decisionDates, setDecisionDates] = useState<Record<string, { decision: string; version_no: string; reason?: string; decided_ts?: string }>>({});

  const strategyAgents = useMemo(() => agents.filter((a) => a.role === "strategy"), [agents]);

  const selectedId = useMemo(() => {
    const p = params.get("agent");
    if (p && strategyAgents.some((a) => a.id === p)) return p;
    return strategyAgents.find((a) => a.status === "running")?.id ?? strategyAgents[0]?.id;
  }, [params, strategyAgents]);

  const agentInfo = useMemo(
    () => strategyAgents.find((a) => a.id === selectedId) ?? null,
    [strategyAgents, selectedId],
  );

  const reportAccountOptions = useMemo(() => {
    if (!agentInfo) return [];
    const items = windows?.items ?? [];
    const seen = new Set<string>();
    const wins = items
      .filter((w) => w.validation_account_id && !seen.has(w.validation_account_id) && seen.add(w.validation_account_id))
      .sort((a, b) => (b.decided_ts || b.created_ts || "").localeCompare(a.decided_ts || a.created_ts || ""));
    return [
      { value: "main", label: `${agentInfo.name} · 主账户` },
      ...wins.map((w) => ({
        value: w.validation_account_id,
        label: `验证窗 ${w.version_no} · ${w.validation_account_id}`,
      })),
    ];
  }, [agentInfo, windows]);

  const loadReportArea = useCallback(
    async (accountId: string, isMain: boolean) => {
      try {
        const tl = await listReportTimeline(accountId);
        setTimeline(tl.reports);
        const recent = tl.reports.slice(0, 10);
        const detail = await Promise.all(
          recent.map((r) =>
            fetchReportVersions(accountId, r.trade_date).then(
              (d) => ({ date: r.trade_date, versions: d.versions }),
              () => ({ date: r.trade_date, versions: [] }),
            ),
          ),
        );
        const unsettled: string[] = [];
        const decisions: Record<string, { decision: string; version_no: string; reason?: string; decided_ts?: string }> = {};
        for (const d of detail) {
          for (const v of d.versions) {
            const wd = v.data_section?.annotations?.window_decision;
            if (wd && !decisions[d.date]) {
              decisions[d.date] = {
                decision: wd.decision,
                version_no: wd.version_no,
                reason: wd.reason,
                decided_ts: wd.decided_ts,
              };
            }
          }
          if (d.versions.some((v) => v.data_section?.annotations?.unsettled)) unsettled.push(d.date);
        }
        setUnsettledDates(isMain ? unsettled : []);
        setDecisionDates(decisions);
      } catch (e) {
        message.error((e as Error).message ?? "加载日报历史失败");
      }
    },
    [message],
  );

  const reload = useCallback(async () => {
    if (!selectedId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const [acct, h, o, t, f] = await Promise.all([
        getAccount(selectedId),
        listAccountHoldings(selectedId),
        listAccountConditionOrders(selectedId),
        listAccountTrades(selectedId),
        listFrozen(selectedId),
      ]);
      setAccount(acct);
      setHoldings(h.holdings);
      setOrders(o.condition_orders);
      setTrades(t.trades);
      setFrozen(f.frozen);
      await loadReportArea(selectedId, true);
    } catch (e) {
      message.error((e as Error).message ?? "加载策略详情失败");
    } finally {
      setLoading(false);
    }
  }, [selectedId, message, loadReportArea]);

  useEffect(() => {
    let active = true;
    void listAgents()
      .then((a) => {
        if (!active) return;
        setAgents(a.agents);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (strategyAgents.length > 0 && !params.get("agent")) {
      const first = strategyAgents.find((a) => a.status === "running")?.id ?? strategyAgents[0]?.id;
      if (first) setParams({ agent: first }, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyAgents.length]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    setReportScope("main");
  }, [selectedId]);

  const switchReportScope = (v: string) => {
    if (!v || v === "main") {
      setReportScope("main");
      setUnsettledDates([]);
      setDecisionDates({});
      void reload();
      return;
    }
    if (!reportAccountOptions.some((o) => o.value === v)) return;
    setReportScope(v);
    setUnsettledDates([]);
    setDecisionDates({});
    void loadReportArea(v, false);
  };

  useEffect(() => {
    if (!selectedId) return;
    let active = true;
    setP2Loading(true);
    void Promise.allSettled([
      fetchStrategyMetrics(selectedId),
      fetchEquityCurve(selectedId, range),
      fetchEvolution(selectedId),
      fetchStrategyProfile(selectedId),
      fetchExitTrackings(selectedId),
      fetchStrategyMemory(selectedId),
      fetchStrategyVersions(selectedId),
      fetchValidationWindows(selectedId),
    ]).then(([m, c, e, sp, et, mem, ver, win]) => {
      if (!active) return;
      setMetrics(m.status === "fulfilled" ? m.value : null);
      setCurve(c.status === "fulfilled" ? c.value : null);
      setEvolution(e.status === "fulfilled" ? e.value : null);
      setProfile(sp.status === "fulfilled" ? sp.value : null);
      setExitTracks(et.status === "fulfilled" ? et.value : null);
      setMemory(mem.status === "fulfilled" ? mem.value : null);
      setVersions(ver.status === "fulfilled" ? ver.value : null);
      setWindows(win.status === "fulfilled" ? win.value : null);
      if (m.status === "rejected" && c.status === "rejected" && e.status === "rejected") {
        console.warn("P2 数据源不可用（spec-06 §6.4），等 EOD 结算产出后再现。");
      }
      setP2Loading(false);
    });
    return () => {
      active = false;
    };
  }, [selectedId, range]);

  useEffect(() => {
    if (!selectedId) return;
    let live = true;
    let inflight = false;
    const poll = async () => {
      if (inflight) return;
      inflight = true;
      try {
        const w = await fetchValidationWindows(selectedId);
        if (live) setWindows(w);
      } catch {
        // 单次失败静默降级，保留下一次轮询
      } finally {
        inflight = false;
      }
    };
    const t = window.setInterval(() => {
      void poll();
    }, 10000);
    return () => {
      live = false;
      window.clearInterval(t);
    };
  }, [selectedId]);

  const mainStatus = account?.status;
  const isFrozen = mainStatus === "paused_buy" || mainStatus === "halted";
  const isTrial = mainStatus === "trial" || agentInfo?.status === "trial";

  const doControl = (op: ControlOp) => {
    if (!agentInfo) return;
    const meta = {
      pause_buy: { title: `冻结买入 · ${agentInfo.name}`, text: "买入即时冻结（保留卖出与风控）。生效后详情页顶部显示冻结横幅。" },
      halt: { title: `熔断冻结 · ${agentInfo.name}`, text: "买卖全停（保留结算与风控）。生效后详情页顶部显示冻结横幅。" },
      resume: { title: `解除冻结 · ${agentInfo.name}`, text: "主账户恢复 normal，可正常下单。" },
    }[op];
    modal.confirm({
      title: meta.title,
      content: <Typography.Text type="secondary">{meta.text}</Typography.Text>,
      okText: op === "resume" ? "恢复" : "确认",
      okButtonProps: { type: op === "resume" ? "primary" : "default", danger: op !== "resume" },
      onOk: async () => {
        try {
          const r = await controlAgent(agentInfo.id, op);
          message.success(`已生效：${r.from} → ${r.to}`);
          void reload();
        } catch (e) {
          message.error((e as Error).message ?? "直控失败");
        }
      },
    });
  };

  const doUnfreeze = (row: FrozenSecurity) => {
    modal.confirm({
      title: `解除冻结 ${row.symbol}？`,
      content: "解除后恢复该 Agent 买入此证券。",
      okText: "解除冻结",
      okButtonProps: { type: "primary" },
      onOk: async () => {
        try {
          await unfreezeSecurity(row.agent_id, row.symbol);
          message.success(`已解除 ${row.symbol}`);
          void reload();
        } catch (e) {
          message.error((e as Error).message ?? "解除失败");
        }
      },
    });
  };

  /** B7 持仓-条件单关联视图：每股持仓挂出的卖出保护单 + 最近结算日成交（spec-06 §6.4） */
  const PROTECT_ACTIVE = ["active", "partial"];
  const lastSettleDate = trades.reduce((acc, t) => (t.settle_date > acc ? t.settle_date : acc), "");
  const protectOrdersOf = (symbol: string) =>
    orders.filter(
      (o) =>
        o.direction === "sell" &&
        PROTECT_ACTIVE.includes(o.status) &&
        (o.symbol === symbol || (o.symbols ?? []).includes(symbol)),
    );
  const dayTradesOf = (symbol: string) =>
    lastSettleDate ? trades.filter((t) => t.symbol === symbol && t.settle_date === lastSettleDate) : [];

  const holdingProtectContent = (h: HoldingInfo) => {
    const prot = protectOrdersOf(h.symbol);
    const day = dayTradesOf(h.symbol);
    return (
      <div style={{ maxWidth: 340 }}>
        <Typography.Text strong style={{ fontSize: 12 }}>
          保护单（已挂 {prot.length}） · {h.symbol}
        </Typography.Text>
        {prot.length ? (
          <Flex vertical gap={2} style={{ marginTop: 6 }}>
            {prot.map((o) => (
              <Typography.Text key={o.id} style={{ fontSize: 12 }} ellipsis>
                {OT_LABEL[o.order_type] ?? o.order_type} · {o.trigger || "触发即市价"}（
                {CO_STATUS[o.status]?.text ?? o.status}）
              </Typography.Text>
            ))}
          </Flex>
        ) : (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            该持仓未挂卖出保护（止盈/止损/移动止损）
          </Typography.Text>
        )}
        <Typography.Text strong style={{ fontSize: 12, display: "block", marginTop: 8 }}>
          当日成交 · {lastSettleDate || "—"}
        </Typography.Text>
        {day.length ? (
          <Flex vertical gap={2} style={{ marginTop: 4 }}>
            {day.map((t) => (
              <Typography.Text key={t.id} style={{ fontSize: 12 }}>
                {t.side === "buy" ? "买入" : "卖出"} {qty(t.qty)} × {price(t.price)} @ {t.trade_time}
              </Typography.Text>
            ))}
          </Flex>
        ) : (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            当日无成交
          </Typography.Text>
        )}
      </div>
    );
  };

  const holdingColumns: ColumnsType<HoldingInfo> = [
    { title: "代码", dataIndex: "symbol", width: 110, render: (s: string) => <Typography.Text code>{s}</Typography.Text> },
    {
      title: "持仓数量", dataIndex: "quantity", width: 120, align: "right", render: qty,
    },
    { title: "均价", dataIndex: "avg_cost", width: 120, align: "right", render: price },
    {
      title: "持仓市值", key: "mktval", width: 140, align: "right",
      render: (_, h) => money(String(Number(h.quantity) * Number(h.avg_cost))),
    },
    { title: "批次", dataIndex: "lots", width: 90, align: "right", render: (l: HoldingInfo["lots"]) => l?.length ?? 0 },
    {
      title: "安全感", key: "protect", width: 110, align: "center",
      render: (_, h) => {
        const prot = protectOrdersOf(h.symbol);
        const remaining = Number(h.quantity || "0");
        const body = holdingProtectContent(h);
        const noData = prot.length === 0 && dayTradesOf(h.symbol).length === 0;
        const tag =
          remaining <= 0 ? (
            <Tag>已清仓</Tag>
          ) : prot.length ? (
            <Tag color="green">已保护 {prot.length}</Tag>
          ) : (
            <Tag color="orange">无保护</Tag>
          );
        return noData ? (
          <Tooltip title="该票无保护单且当日无成交——悬停查看 B7 关联明细">
            {tag}
          </Tooltip>
        ) : (
          <Popover trigger="hover" placement="left" title="持仓-条件单关联（spec-06 §6.4 B7）" content={body}>
            <span style={{ cursor: "default" }}>{tag}</span>
          </Popover>
        );
      },
    },
    { title: "更新时间", dataIndex: "updated_ts", render: (v: string) => fmtBeijingTime(v) },
  ];

  const orderColumns: ColumnsType<ConditionOrderInfo> = [
    {
      title: "类型", dataIndex: "order_type", width: 130,
      render: (t: string, r) => <Tag color={r.direction === "buy" ? "red" : "green"}>{OT_LABEL[t] ?? t}</Tag>,
    },
    { title: "标的", dataIndex: "symbol", width: 120, render: (s: string) => s || <Typography.Text type="secondary">—</Typography.Text> },
    { title: "价格类型", dataIndex: "price_type", width: 100, render: (t: string) => (t === "limit" ? "限价" : "市价") },
    { title: "触发", dataIndex: "trigger", ellipsis: true, render: (v: string) => v || "—" },
    {
      title: "状态", dataIndex: "status", width: 110,
      render: (s: string) => {
        const m = CO_STATUS[s] ?? { color: "default", text: s };
        return <Tag color={m.color}>{m.text}</Tag>;
      },
    },
    {
      title: "有效期", dataIndex: "validity", width: 120,
      render: (v: string, r) => (v === "until" ? `至 ${r.valid_until}` : v === "today" ? "当日有效" : v === "long" ? "长期有效" : v),
    },
    { title: "理由", dataIndex: "reason", ellipsis: true, render: (v: string) => v || "—" },
    { title: "创建时间", dataIndex: "created_at", width: 150, render: (v: string) => fmtBeijingTime(v) },
  ];

  const tradeColumns: ColumnsType<TradeInfo> = [
    { title: "代码", dataIndex: "symbol", width: 100, render: (s: string) => <Typography.Text code>{s}</Typography.Text> },
    {
      title: "方向", dataIndex: "side", width: 80,
      render: (s: string) => (s === "buy" ? <Tag color="red">买入</Tag> : <Tag color="green">卖出</Tag>),
    },
    { title: "数量", dataIndex: "qty", width: 110, align: "right", render: qty },
    { title: "成交价", dataIndex: "price", width: 110, align: "right", render: price },
    { title: "成交额", dataIndex: "amount", width: 130, align: "right", render: money },
    { title: "费用", dataIndex: "fee_total", width: 110, align: "right", render: money },
    {
      title: "质量", dataIndex: "quality", width: 100,
      render: (q: string) => (q ? <Tag color="orange">{q === "degraded" ? "降级" : q}</Tag> : <Typography.Text type="secondary">—</Typography.Text>),
    },
    { title: "结算日", dataIndex: "settle_date", width: 110 },
    { title: "成交时刻", dataIndex: "trade_time", render: (v: string) => v },
  ];

  const empty = <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={EMPTY_NOTE} style={{ padding: "24px 0" }} />;

  if (loading && !account) {
    return (
      <div style={{ padding: 16 }}>
        <Skeleton active paragraph={{ rows: 10 }} />
      </div>
    );
  }

  const activeOrders = orders.filter((o) => o.status === "active").length;
  const todayPnl = account?.today_pnl ?? "0";

  return (
    <div style={{ padding: 16, minHeight: "100%" }}>
      <Flex justify="space-between" align="flex-start" gap={12} wrap style={{ marginBottom: 12 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            策略详情
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
             单策略聚合视图：持仓 / 条件单 / 流水 / 日报 + 业绩指标 / 资金曲线（沪深300 基准）/ 演进账本（spec-06 §6.4）
          </Typography.Text>
        </div>
        <Flex gap={8} align="center">
          {account && (
            <Button
              icon={<LinkOutlined />}
              onClick={() => navigate("/control")}
            >
              去直控台
            </Button>
          )}
          <Select
            style={{ minWidth: 240 }}
            value={selectedId}
            onChange={(v) => setParams({ agent: v }, { replace: true })}
            showSearch
            optionFilterProp="label"
            options={strategyAgents.map((a) => ({
              value: a.id,
              label: a.status === "running" ? `${a.name}（运行中）` : a.name,
            }))}
          />
        </Flex>
      </Flex>

      {!agentInfo && !loading ? (
        <Card>
          <Empty description="暂无策略 Agent——请先在 Agent 看板创建策略并运行" />
        </Card>
      ) : agentInfo ? (
        <>
          {isTrial && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="试运行中（历史回放，L2 近似撮合）"
              description="该账户处于试运行验收期，业绩口径为 L2 近似撮合；上线复位后横幅消失。验收进度与报告入口见 Agent 看板。"
              action={
                <Button size="small" onClick={() => navigate("/agents")}>
                  查看验收看板
                </Button>
              }
            />
          )}
          {isFrozen && (
            <Alert
              type={mainStatus === "halted" ? "error" : "warning"}
              showIcon
              icon={<StopOutlined />}
              style={{ marginBottom: 12 }}
              message={mainStatus === "halted" ? "已熔断冻结（买卖全停）" : "已冻结买入（保留卖出与风控）"}
              description="状态由用户直控台即时下发。解除后横幅消失。"
              action={
                <Button size="small" type="primary" ghost icon={<CaretRightOutlined />} onClick={() => doControl("resume")}>
                  解除冻结
                </Button>
              }
            />
          )}
          {unsettledDates.length > 0 && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message={`存在未结算交易日：${unsettledDates.join("、")}`}
              description="当日尚无结算产物快照（settlement_log 未落），数据段为账户当前现值口径（spec-04 §5.2 数据段零 token 补齐）。"
            />
          )}

          <StrategyProfileCard profile={profile} loading={p2Loading} />

          <Card size="small" style={{ marginBottom: 12 }} title={`模拟账户 · ${agentInfo.name}`}>
            <Flex gap={24} wrap align="center" style={{ marginBottom: 4 }}>
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>NAV（份额法）</Typography.Text>
                <div style={{ fontSize: 22, fontWeight: 600 }}>{account ? money(account.nav) : "—"}</div>
              </div>
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>可用现金</Typography.Text>
                <div style={{ fontSize: 18, fontWeight: 600 }}>{account ? money(account.cash) : "—"}</div>
              </div>
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>今日盈亏</Typography.Text>
                <div style={{ fontSize: 18, fontWeight: 600, color: pnlColor(todayPnl) }}>{signed(todayPnl)}</div>
              </div>
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>累计盈亏</Typography.Text>
                <div style={{ fontSize: 18, fontWeight: 600, color: pnlColor(account?.total_pnl ?? "0") }}>{signed(account?.total_pnl ?? "0")}</div>
              </div>
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>生效中条件单</Typography.Text>
                <div style={{ fontSize: 18, fontWeight: 600 }}>{activeOrders}</div>
              </div>
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>冻结证券</Typography.Text>
                <div style={{ fontSize: 18, fontWeight: 600, color: frozen.length ? "#fa8c16" : undefined }}>
                  <LockOutlined /> {frozen.length}
                </div>
              </div>
            </Flex>
            <Flex gap={8} wrap style={{ marginTop: 8 }}>
              {!isFrozen ? (
                <>
                  <Button size="small" icon={<PauseCircleOutlined />} onClick={() => doControl("pause_buy")}>冻结买入</Button>
                  <Button size="small" danger icon={<StopOutlined />} onClick={() => doControl("halt")}>熔断冻结</Button>
                </>
              ) : (
                <Button size="small" type="primary" ghost icon={<CaretRightOutlined />} onClick={() => doControl("resume")}>
                  解除冻结 / 恢复
                </Button>
              )}
              <Tooltip title="紧急清仓请到直控台（L3 输入确认）">
                <Button size="small" danger ghost icon={<ClearOutlined />} onClick={() => navigate("/control")}>
                  紧急清仓
                </Button>
              </Tooltip>
            </Flex>
            {frozen.length > 0 && (
              <Flex vertical gap={4} style={{ marginTop: 8 }}>
                {frozen.map((f) => (
                  <Flex key={f.symbol} gap={8} align="center">
                    <Tag color="orange" icon={<LockOutlined />} style={{ marginInlineEnd: 0 }}>
                      {f.symbol}
                    </Tag>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }} ellipsis>
                      {f.reason || "（未填原因）"}
                    </Typography.Text>
                    <Button size="small" type="link" style={{ padding: 0, fontSize: 12 }} onClick={() => doUnfreeze(f)}>
                      解除
                    </Button>
                  </Flex>
                ))}
              </Flex>
            )}
          </Card>

          <MetricSummaryCard metrics={metrics} loading={p2Loading} />
          <EquityCurveCard
            agentName={agentInfo.name}
            curve={curve}
            loading={p2Loading}
            range={range}
            onRangeChange={setRange}
          />

          <Card
            size="small"
            title="持仓明细"
            style={{ marginBottom: 12 }}
            extra={
              unsettledDates.length ? (
                <Tag color="orange">有未结算交易日</Tag>
              ) : undefined
            }
          >
            <Table<HoldingInfo>
              rowKey="id" size="small" columns={holdingColumns} dataSource={holdings} pagination={false}
              locale={{ emptyText: empty }} scroll={{ x: 760 }}
              expandable={{
                expandedRowRender: (h) =>
                  h.lots?.length ? (
                    <Table
                      rowKey="id" size="small" pagination={false}
                      dataSource={h.lots}
                      columns={[
                        { title: "批次", dataIndex: "id" },
                        { title: "买入日期", dataIndex: "buy_date" },
                        { title: "买入价", dataIndex: "buy_price", align: "right", render: price },
                        { title: "数量", dataIndex: "quantity", align: "right", render: qty },
                        { title: "剩余", dataIndex: "remaining", align: "right", render: qty },
                        { title: "版本", dataIndex: "strategy_version_no", width: 90 },
                      ]}
                    />
                  ) : (
                    <Typography.Text type="secondary">—</Typography.Text>
                  ),
              }}
            />
          </Card>

          <Card size="small" title="条件单" style={{ marginBottom: 12 }}>
            <Table<ConditionOrderInfo>
              rowKey="id" size="small" columns={orderColumns} dataSource={orders} pagination={{ pageSize: 10, hideOnSinglePage: true }}
              locale={{ emptyText: empty }} scroll={{ x: 1080 }}
            />
          </Card>

          <Card size="small" title="交易流水" style={{ marginBottom: 12 }}>
            <Table<TradeInfo>
              rowKey="id" size="small" columns={tradeColumns} dataSource={trades} pagination={{ pageSize: 10, hideOnSinglePage: true }}
              locale={{ emptyText: empty }} scroll={{ x: 1060 }}
            />
          </Card>

          <Card
            size="small"
            title={reportScope === "main" ? "日报历史" : "日报历史 · 验证窗账户"}
            extra={
              <Flex gap={8} align="center">
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  账户
                </Typography.Text>
                <Select
                  size="small"
                  style={{ width: 220 }}
                  value={reportScope}
                  options={reportAccountOptions}
                  onChange={switchReportScope}
                />
                <Button size="small" type="link" onClick={() => navigate("/reports")}>
                  日报中心 <ArrowRightOutlined />
                </Button>
              </Flex>
            }
          >
            <Table<ReportTimelineEntry>
              rowKey="trade_date" size="small" pagination={false}
              dataSource={timeline}
              locale={{
                emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无日报记录——结算产出后自动生成（spec-04 §5）" />,
              }}
              columns={[
                { title: "交易日", dataIndex: "trade_date", width: 140 },
                {
                  title: "状态", dataIndex: "status", width: 140,
                  render: (s: ReportTimelineEntry["status"]) => {
                    const m = { normal: { c: "green", t: "正常" }, absent: { c: "red", t: "缺勤" }, resend: { c: "orange", t: "修订补发" } }[s];
                    return <Tag color={m.c}>{m.t}</Tag>;
                  },
                },
                {
                  title: "角标", key: "badge", width: 190,
                  render: (_, r) => {
                    const wd = decisionDates[r.trade_date];
                    if (wd) {
                      const dlabel = { activate: "晋升现役", rollback: "否决候选", sealed: "封存留证" }[wd.decision] ?? wd.decision ?? "收口";
                      return (
                        <Tooltip
                          title={`注解版 v${wd.version_no} → ${dlabel}（${wd.reason ?? "未记录理由"}）`}
                        >
                          <Tag color="purple" style={{ marginInlineEnd: 0 }}>EVOQUANT 收口</Tag>
                        </Tooltip>
                      );
                    }
                    if (unsettledDates.includes(r.trade_date)) {
                      return <Tag color="orange">未结算</Tag>;
                    }
                    return <Typography.Text type="secondary">—</Typography.Text>;
                  },
                },
                { title: "版本", dataIndex: "latest_version", width: 80, align: "right" },
                { title: "最新生成", dataIndex: "latest_created_ts", render: (v: string) => fmtBeijingTime(v) },
              ]}
            />
          </Card>

          <StrategyEvolutionCard
            evolution={evolution}
            memory={memory}
            windows={windows}
            loading={p2Loading}
          />

          <StrategyVersionsCard versions={versions} windows={windows} loading={p2Loading} />

          <ValidationWindowsCard windows={windows} loading={p2Loading} />

          <SellTrackingCard data={exitTracks} loading={p2Loading} />

          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 4 }}>
            关联只读视图已就位：持仓保护单与结算日成交、理念章程、演进记忆、EVOQUANT 版本状态机（spec-02 §3.1 / §9）。
            写方=引擎结算与能力/章程管理入口；读侧零 token，无需额外说明即可自解释。
          </Typography.Paragraph>
        </>
      ) : null}
    </div>
  );
}

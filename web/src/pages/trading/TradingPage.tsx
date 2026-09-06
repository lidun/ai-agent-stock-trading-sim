import { useCallback, useEffect, useMemo, useState } from "react";
import {
  App as AntApp,
  Button,
  Card,
  Empty,
  Flex,
  Select,
  Skeleton,
  Table,
  Tag,
  Typography,
  theme as antTheme,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import {
  listAccountConditionOrders,
  listAccountHoldings,
  listAccountSettlements,
  listAccountTrades,
  listAccounts,
  type AccountInfo,
  type ConditionOrderInfo,
  type HoldingInfo,
  type SettlementInfo,
  type TradeInfo,
} from "../../api/endpoints";
import { fmtBeijingTime } from "../../utils/time";

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

const VALIDITY_LABEL: Record<string, string> = {
  today: "当日有效",
  until: "至指定日",
  long: "长期有效",
};

function price(v: string): string {
  return Number(v || "0").toLocaleString("zh-CN", {
    minimumFractionDigits: 3,
    maximumFractionDigits: 3,
  });
}

function qty(v: string): string {
  return Number(v || "0").toLocaleString("zh-CN", {
    maximumFractionDigits: 4,
  });
}

export default function TradingPage() {
  const { token } = antTheme.useToken();
  const { message } = AntApp.useApp();

  const [accounts, setAccounts] = useState<AccountInfo[]>([]);
  const [holdings, setHoldings] = useState<HoldingInfo[]>([]);
  const [orders, setOrders] = useState<ConditionOrderInfo[]>([]);
  const [trades, setTrades] = useState<TradeInfo[]>([]);
  const [settlements, setSettlements] = useState<SettlementInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [acctFilter, setAcctFilter] = useState<string>("all");

  const accountName = useMemo(() => {
    const m = new Map<string, string>();
    accounts.forEach((a) => m.set(a.id, a.agent_name));
    return m;
  }, [accounts]);

  const reload = useCallback(async () => {
    try {
      const list = await listAccounts();
      const strategyAccs = list.accounts.filter((a) => a.agent_role === "strategy");
      setAccounts(list.accounts);
      const [hViews, oViews, tViews, sViews] = await Promise.all([
        Promise.all(strategyAccs.map((a) => listAccountHoldings(a.agent_id))),
        Promise.all(strategyAccs.map((a) => listAccountConditionOrders(a.agent_id))),
        Promise.all(strategyAccs.map((a) => listAccountTrades(a.agent_id))),
        Promise.all(strategyAccs.map((a) => listAccountSettlements(a.agent_id))),
      ]);
      setHoldings(hViews.flatMap((v) => v.holdings));
      setOrders(oViews.flatMap((v) => v.condition_orders));
      setTrades(tViews.flatMap((v) => v.trades));
      setSettlements(sViews.flatMap((v) => v.settlements));
    } catch (e) {
      message.error((e as Error).message ?? "加载失败");
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const filteredHoldings = useMemo(
    () => (acctFilter === "all" ? holdings : holdings.filter((h) => h.account_id === acctFilter)),
    [holdings, acctFilter],
  );
  const filteredOrders = useMemo(
    () => (acctFilter === "all" ? orders : orders.filter((o) => o.account_id === acctFilter)),
    [orders, acctFilter],
  );
  const filteredTrades = useMemo(
    () => (acctFilter === "all" ? trades : trades.filter((t) => t.account_id === acctFilter)),
    [trades, acctFilter],
  );
  const filteredSettles = useMemo(
    () =>
      acctFilter === "all"
        ? settlements
        : settlements.filter((s) => s.account_id === acctFilter),
    [settlements, acctFilter],
  );

  const holdingColumns: ColumnsType<HoldingInfo> = [
    { title: "账户", dataIndex: "account_id", width: 120, render: (id: string) => accountName.get(id) ?? id },
    { title: "代码", dataIndex: "symbol", width: 100, render: (s: string) => <Typography.Text code>{s}</Typography.Text> },
    { title: "持仓数量", dataIndex: "quantity", width: 120, align: "right", render: qty },
    { title: "均价", dataIndex: "avg_cost", width: 120, align: "right", render: price },
    { title: "批次", dataIndex: "lots", width: 80, align: "right", render: (l: HoldingInfo["lots"]) => l?.length ?? 0 },
    { title: "更新时间", dataIndex: "updated_ts", render: (v: string) => fmtBeijingTime(v) },
  ];

  const orderColumns: ColumnsType<ConditionOrderInfo> = [
    { title: "账户", dataIndex: "account_id", width: 120, render: (id: string) => accountName.get(id) ?? id },
    {
      title: "类型",
      dataIndex: "order_type",
      width: 110,
      render: (t: string, r) => <Tag color={r.direction === "buy" ? "red" : "green"}>{OT_LABEL[t] ?? t}</Tag>,
    },
    { title: "标的", dataIndex: "symbol", width: 110, render: (s: string) => s || <Typography.Text type="secondary">—</Typography.Text> },
    { title: "价格类型", dataIndex: "price_type", width: 100, render: (t: string) => (t === "limit" ? "限价" : "市价") },
    { title: "触发", dataIndex: "trigger", ellipsis: true, render: (v: string) => v || "—" },
    {
      title: "状态",
      dataIndex: "status",
      width: 100,
      render: (s: string) => {
        const m = CO_STATUS[s] ?? { color: "default", text: s };
        return <Tag color={m.color}>{m.text}</Tag>;
      },
    },
    {
      title: "有效期",
      dataIndex: "validity",
      width: 130,
      render: (v: string, r) => (v === "until" ? `至 ${r.valid_until}` : VALIDITY_LABEL[v] ?? v),
    },
    { title: "理由", dataIndex: "reason", ellipsis: true, render: (v: string) => v || "—" },
    { title: "创建时间", dataIndex: "created_at", width: 150, render: (v: string) => fmtBeijingTime(v) },
  ];

  const tradeColumns: ColumnsType<TradeInfo> = [
    { title: "账户", dataIndex: "account_id", width: 120, render: (id: string) => accountName.get(id) ?? id },
    { title: "代码", dataIndex: "symbol", width: 90, render: (s: string) => <Typography.Text code>{s}</Typography.Text> },
    {
      title: "方向",
      dataIndex: "side",
      width: 70,
      render: (s: string) => (s === "buy" ? <Tag color="red">买入</Tag> : <Tag color="green">卖出</Tag>),
    },
    { title: "数量", dataIndex: "qty", width: 100, align: "right", render: qty },
    { title: "成交价", dataIndex: "price", width: 100, align: "right", render: price },
    {
      title: "成交额",
      dataIndex: "amount",
      width: 120,
      align: "right",
      render: (v: string) => money2(v),
    },
    { title: "费用", dataIndex: "fee_total", width: 110, align: "right", render: (v: string) => money2(v) },
    {
      title: "质量",
      dataIndex: "quality",
      width: 90,
      render: (q: string) =>
        q ? <Tag color="orange">{q === "degraded" ? "降级" : q}</Tag> : <Typography.Text type="secondary">—</Typography.Text>,
    },
    { title: "结算日", dataIndex: "settle_date", width: 110 },
    { title: "成交时刻", dataIndex: "trade_time", render: (v: string) => v },
  ];

  const settleColumns: ColumnsType<SettlementInfo> = [
    { title: "账户", dataIndex: "account_id", width: 140, render: (id: string) => accountName.get(id) ?? id },
    { title: "交易日", dataIndex: "trade_date", width: 110 },
    {
      title: "按票档位",
      dataIndex: "granularity_used",
      render: (g: Record<string, string>) =>
        Object.entries(g).length ? (
          <Flex gap={4} wrap>
            {Object.entries(g).map(([sym, tier]) => (
              <Tag key={sym} style={{ marginInlineEnd: 0 }}>
                {sym}: {tier.toUpperCase()}
              </Tag>
            ))}
          </Flex>
        ) : (
          <Typography.Text type="secondary">—</Typography.Text>
        ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      render: (s: string) => <Tag color="green">{s === "done" ? "已完成" : s}</Tag>,
    },
    { title: "settle_key", dataIndex: "settle_key", ellipsis: true },
    { title: "结算时间", dataIndex: "created_at", width: 160, render: (v: string) => fmtBeijingTime(v) },
  ];

  function money2(v: string): string {
    return Number(v || "0").toLocaleString("zh-CN", {
      style: "currency",
      currency: "CNY",
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  const emptyNote = (
    <Empty
      description="撮合引擎尚未写入数据——EOD 回放结算落地后，持仓/条件单将在此呈现（spec-01 §2.2/§2.3）"
      style={{ padding: "32px 0" }}
    />
  );

  if (loading) {
    return (
      <div style={{ padding: 16 }}>
        <Skeleton active paragraph={{ rows: 8 }} />
      </div>
    );
  }

  return (
    <div style={{ padding: 16, minHeight: "100%" }}>
      <Flex justify="space-between" align="flex-start" gap={12} wrap>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            交易中心
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            跨 Agent 持仓/条件单/成交/结算日志只读视图（引擎写入后呈现）；回放可视化见 spec-06 §6.5（P3）
          </Typography.Text>
        </div>
        <Flex gap={8} align="center">
          <Select
            size="middle"
            style={{ width: 220 }}
            value={acctFilter}
            onChange={setAcctFilter}
            options={[
              { value: "all", label: "全部策略账户" },
              ...accounts.map((a) => ({ value: a.agent_id, label: a.agent_name })),
            ]}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void reload()}>
            刷新
          </Button>
        </Flex>
      </Flex>

      <Flex gap={12} wrap style={{ margin: "12px 0" }}>
        <Card size="small" style={{ flex: 1, minWidth: 180 }} styles={{ body: { padding: "8px 14px" } }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            持仓标的
          </Typography.Text>
          <div style={{ fontSize: 20, fontWeight: 600, color: token.colorText }}>
            {filteredHoldings.length}
          </div>
        </Card>
        <Card size="small" style={{ flex: 1, minWidth: 180 }} styles={{ body: { padding: "8px 14px" } }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            生效中条件单
          </Typography.Text>
          <div style={{ fontSize: 20, fontWeight: 600, color: token.colorText }}>
            {filteredOrders.filter((o) => o.status === "active").length}
          </div>
        </Card>
        <Card size="small" style={{ flex: 1, minWidth: 180 }} styles={{ body: { padding: "8px 14px" } }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            成交笔数
          </Typography.Text>
          <div style={{ fontSize: 20, fontWeight: 600, color: token.colorText }}>
            {filteredTrades.length}
          </div>
        </Card>
        <Card size="small" style={{ flex: 1, minWidth: 180 }} styles={{ body: { padding: "8px 14px" } }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            已结算交易日
          </Typography.Text>
          <div style={{ fontSize: 20, fontWeight: 600, color: token.colorText }}>
            {new Set(filteredSettles.map((s) => s.trade_date)).size}
          </div>
        </Card>
      </Flex>

      <Card size="small" title="持仓与批次（spec-01 §2.2）" style={{ marginBottom: 12 }}>
        <Table<HoldingInfo>
          rowKey="id"
          size="small"
          columns={holdingColumns}
          dataSource={filteredHoldings}
          pagination={false}
          locale={{ emptyText: emptyNote }}
          scroll={{ x: 720 }}
        />
      </Card>

      <Card size="small" title="条件单（spec-01 §2.3）" style={{ marginBottom: 12 }}>
        <Table<ConditionOrderInfo>
          rowKey="id"
          size="small"
          columns={orderColumns}
          dataSource={filteredOrders}
          pagination={false}
          locale={{ emptyText: emptyNote }}
          scroll={{ x: 1000 }}
        />
      </Card>

      <Card size="small" title="成交记录（spec-01 §2.5 trades）" style={{ marginBottom: 12 }}>
        <Table<TradeInfo>
          rowKey="id"
          size="small"
          columns={tradeColumns}
          dataSource={filteredTrades}
          pagination={false}
          locale={{
            emptyText: (
              <Empty
                description="EOD 引擎结算后按日写入成交明细；行情数据源接入后自动呈现"
                style={{ padding: "24px 0" }}
              />
            ),
          }}
          scroll={{ x: 1100 }}
        />
      </Card>

      <Card size="small" title="结算日志（spec-01 §2.5 settlement_log）">
        <Table<SettlementInfo>
          rowKey="id"
          size="small"
          columns={settleColumns}
          dataSource={filteredSettles}
          pagination={false}
          locale={{
            emptyText: (
              <Empty
                description="尚无 EOD 结算记录——每个交易日完成结算后写入（settle_key 幂等）"
                style={{ padding: "24px 0" }}
              />
            ),
          }}
          scroll={{ x: 1000 }}
        />
      </Card>
    </div>
  );
}

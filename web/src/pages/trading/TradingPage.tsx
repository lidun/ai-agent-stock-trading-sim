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
  listAccounts,
  type AccountInfo,
  type ConditionOrderInfo,
  type HoldingInfo,
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
      const [hViews, oViews] = await Promise.all([
        Promise.all(strategyAccs.map((a) => listAccountHoldings(a.agent_id))),
        Promise.all(strategyAccs.map((a) => listAccountConditionOrders(a.agent_id))),
      ]);
      setHoldings(hViews.flatMap((v) => v.holdings));
      setOrders(oViews.flatMap((v) => v.condition_orders));
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
            跨 Agent 持仓与条件单只读视图；结算日志与回放可视化见 spec-06 §6.5（P2/P3）
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

      <Card size="small" title="条件单（spec-01 §2.3）">
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
    </div>
  );
}

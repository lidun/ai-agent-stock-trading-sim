import { Card, Empty, Flex, Skeleton, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { ExitTrackingItem, ExitTrackingList } from "../../api/endpoints";
import { fmtBeijing } from "../../utils/time";

interface Props {
  data: ExitTrackingList | null;
  loading: boolean;
}

const CONCLUSION_TAG: Record<string, { color: string; text: string }> = {
  卖对: { color: "green", text: "卖对" },
  卖平: { color: "default", text: "卖平" },
  卖早: { color: "orange", text: "卖早" },
};

function num(v: number | null, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  return v.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function pct(v: number | null): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
}

const columns: ColumnsType<ExitTrackingItem> = [
  {
    title: "标的", dataIndex: "symbol", width: 100,
    render: (s: string) => <Typography.Text code>{s}</Typography.Text>,
  },
  { title: "卖出日", dataIndex: "sell_date", width: 110 },
  { title: "卖出价", dataIndex: "sell_price", width: 100, align: "right", render: (v: number | null) => num(v, 3) },
  { title: "数量", dataIndex: "qty", width: 100, align: "right", render: (v: number | null) => num(v, 0) },
  {
    title: "状态", dataIndex: "status", width: 100,
    render: (s: string, r) =>
      s === "done" ? (
        <Tag color="default">已了结</Tag>
      ) : (
        <Tooltip title={`窗口推进 ${r.sessions_done} 个交易日`}>
          <Tag color="blue">跟踪中</Tag>
        </Tooltip>
      ),
  },
  { title: "跟踪进度", dataIndex: "sessions_done", width: 90, align: "right", render: (v: number) => `${v} 日` },
  {
    title: "区间高 / 低", key: "range", width: 130, align: "right",
    render: (_, r) => (
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {r.period_high != null ? num(r.period_high, 3) : "—"} / {r.period_low != null ? num(r.period_low, 3) : "—"}
      </Typography.Text>
    ),
  },
  {
    title: "前向收益", dataIndex: "fwd_return_pct", width: 100, align: "right",
    render: (v: number | null, r) => (
      <Tooltip title={`沪深300 同期 ${pct(r.bench_return_pct)}，超额 ${pct(r.excess_pct)}`}>
        <span>{pct(v)}</span>
      </Tooltip>
    ),
  },
  {
    title: "结论", dataIndex: "conclusion", width: 90,
    render: (c: string) => {
      const m = CONCLUSION_TAG[c];
      return c ? <Tag color={m?.color}>{m?.text ?? c}</Tag> : <Typography.Text type="secondary">—</Typography.Text>;
    },
  },
  {
    title: "卖出理由", dataIndex: "sell_reason", width: 100, ellipsis: true,
    render: (v: string) => v || "—",
  },
  { title: "了结时间", dataIndex: "done_ts", width: 150, render: (v: string) => (v ? fmtBeijing(v) : "—") },
];

export default function SellTrackingCard({ data, loading }: Props) {
  return (
    <Card size="small" title="卖出跟踪" style={{ marginBottom: 12 }}>
      {loading ? (
        <Skeleton active paragraph={{ rows: 4 }} />
      ) : !data || data.total === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="暂无卖出跟踪样本——卖出成交自动登记（spec-01 §8.1），引擎逐交易日推进后呈现"
          style={{ padding: "16px 0" }}
        />
      ) : (
        <>
          <Flex wrap gap={8} style={{ marginBottom: 8 }}>
            <Tag>共 {data.total} 笔</Tag>
            <Tag color="blue">跟踪中 {data.tracking}</Tag>
            <Tag>已了结 {data.done}</Tag>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              结论口径：卖对=卖出后回落；卖早=卖出后继续上涨；数值为前向收益与相对沪深300 超额（%）。
            </Typography.Text>
          </Flex>
          <Table<ExitTrackingItem>
            rowKey="id"
            size="small"
            columns={columns}
            dataSource={data.items}
            pagination={{ pageSize: 10, hideOnSinglePage: true }}
            scroll={{ x: 1260 }}
          />
        </>
      )}
    </Card>
  );
}

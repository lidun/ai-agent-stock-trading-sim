import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Flex,
  Skeleton,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined } from "@ant-design/icons";
import {
  fetchQualityMonitor,
  type MarketQuality,
  type QualityMonitorDay,
} from "../../api/endpoints";
import { fmtBeijingTime } from "../../utils/time";

const TRADE_QUALITY_META: Record<string, { color: string; text: string }> = {
  official: { color: "green", text: "official 官方对齐" },
  degraded: { color: "orange", text: "degraded 降级" },
  no_bench: { color: "red", text: "no_bench 无基准" },
  stale_close: { color: "red", text: "stale_close 陈旧收盘" },
  none: { color: "default", text: "未标注" },
};

const EXIT_CONCLUSION_LABEL: Record<string, string> = {
  卖对: "卖对", 卖平: "卖平", 卖早: "卖早",
};

const ACC_STATUS_META: Record<string, { color: string; text: string }> = {
  normal: { color: "green", text: "正常" },
  paused_buy: { color: "orange", text: "冻结买入" },
  halted: { color: "red", text: "熔断冻结" },
  trial: { color: "blue", text: "试运行" },
  archived: { color: "default", text: "已归档" },
};

function distRows(dist: Record<string, number>) {
  return Object.entries(dist)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => ({ key: k, name: k, n }));
}

export default function MarketQualityMonitorPage() {
  const [data, setData] = useState<MarketQuality | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setData(await fetchQualityMonitor(60));
    } catch (e) {
      // 页面级只读：失败保留上次视图，错误透传 antd 全局错误边界无需额外提示
      console.error("quality monitor load failed", e);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const empty = !!data && data.days.length === 0 && data.trades.total === 0;

  const dayColumns: ColumnsType<QualityMonitorDay> = [
    { title: "结算日", dataIndex: "trade_date", width: 130 },
    { title: "运行账户数", dataIndex: "runs", width: 110 },
    {
      title: "档位使用分布", dataIndex: "granularity", width: 260,
      render: (g: Record<string, number>) =>
        Object.keys(g).length ? (
          <Space size={4} wrap>
            {Object.entries(g).map(([lvl, n]) => (
              <Tag key={lvl} color={lvl.includes("5m") || lvl.includes("1m") ? "geekblue" : "cyan"}>
                {lvl} × {n}
              </Tag>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">—</Typography.Text>
        ),
    },
    { title: "证券数据段合计", dataIndex: "symbols_total", width: 130 },
    { title: "状态", dataIndex: "status", width: 90, render: () => <Tag color="green">done</Tag> },
  ];

  return (
    <div style={{ padding: 16, minHeight: "100%" }}>
      <Flex justify="space-between" align="flex-start" style={{ marginBottom: 12 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            市场与数据监控
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            真实落库聚合（spec-02 §7 / spec-03）：结算运行 / 成交标签 / 卖出跟踪结清；数据缺位不做伪值
          </Typography.Text>
        </div>
        <Button icon={<ReloadOutlined />} onClick={() => void reload()}>
          刷新
        </Button>
      </Flex>

      {loading ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : !data ? (
        <Card>
          <Empty description="监控聚合不可用——后端只读接口返回异常" style={{ padding: "40px 0" }} />
        </Card>
      ) : (
        <Flex vertical gap={12}>
          <Flex gap={12} wrap>
            <Card size="small" style={{ flex: "1 1 200px" }}>
              <Statistic title="统计窗口（天）" value={data.window_days} />
            </Card>
            <Card size="small" style={{ flex: "1 1 200px" }}>
              <Statistic title="结算日运行记录" value={data.days.length} />
            </Card>
            <Card size="small" style={{ flex: "1 1 200px" }}>
              <Statistic title="证券数据段合计" value={data.settle_symbols_total} />
            </Card>
            <Card size="small" style={{ flex: "1 1 200px" }}>
              <Statistic title="卖出跟踪（完成/在跟踪）" value={data.exits.total}
                suffix={<Typography.Text type="secondary" style={{ fontSize: 13 }}>
                  {data.exits.done}/{data.exits.tracking}
                </Typography.Text>}
              />
            </Card>
            <Card size="small" style={{ flex: "1 1 220px" }}>
              <Statistic title="主账户" value={data.accounts.total}
                suffix={
                  <Space size={4} wrap style={{ marginLeft: 4 }}>
                    {Object.entries(data.accounts.by_status).slice(0, 4).map(([s, n]) => {
                      const m = ACC_STATUS_META[s] ?? { color: "default", text: s };
                      return <Tag key={s} color={m.color}>{m.text} {n}</Tag>;
                    })}
                  </Space>
                }
              />
            </Card>
          </Flex>

          {empty && (
            <Alert
              type="info"
              showIcon
              message="暂无可监控运行痕迹"
              description="EOD 结算引擎运行后（settlement_log/trades/exit_trackings）本聚合自动填充；未出现空运行不视为缺陷。"
            />
          )}

          <Card
            size="small"
            title="结算运行（settlement_log 行内汇总）"
            extra={<Tag color={data.eod_auto_settle ? "green" : "default"}>
              {data.eod_auto_settle ? "自动结算已启用" : "自动结算默认关闭（CORE_EOD_AUTO_SETTLE=1 启用）"}
            </Tag>}
          >
            <Table<QualityMonitorDay>
              rowKey="trade_date"
              size="small"
              dataSource={data.days}
              columns={dayColumns}
              pagination={false}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无结算运行记录" /> }}
              scroll={{ x: 760 }}
            />
          </Card>

          <Flex gap={12} align="stretch" wrap>
            <Card size="small" title="成交质量（trades.quality 标签）" style={{ flex: "1 1 420px", minWidth: 380 }}>
              <TagDistribution
                rows={distRows(data.trades.by_quality)}
                meta={TRADE_QUALITY_META}
                total={data.trades.total}
              />
            </Card>
            <Card size="small" title="卖出跟踪结论（spec-01 §8.1）" style={{ flex: "1 1 420px", minWidth: 380 }}>
              <TagDistribution
                rows={distRows(data.exits.conclusions)}
                meta={Object.fromEntries(
                  Object.keys(EXIT_CONCLUSION_LABEL).map((k) => [
                    k,
                    { color: k === "卖早" ? "red" : k === "卖对" ? "green" : "default", text: EXIT_CONCLUSION_LABEL[k] },
                  ]),
                )}
                total={data.exits.done}
              />
            </Card>
          </Flex>

          <Alert
            type="info"
            showIcon
            message={`行情源：${data.feed.source_family} 单源（spec-03 异族抽检 ${data.feed.cross_family_check}）`}
            description={data.feed.note}
          />
          {data.generated_ts && (
            <Typography.Text type="secondary" style={{ fontSize: 11, alignSelf: "flex-end" }}>
              聚合生成于 {fmtBeijingTime(data.generated_ts)}
            </Typography.Text>
          )}
        </Flex>
      )}
    </div>
  );
}

function TagDistribution({
  rows,
  meta,
  total,
}: {
  rows: { key: string; name: string; n: number }[];
  meta: Record<string, { color: string; text: string }>;
  total: number;
}) {
  if (rows.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="暂无样本——引擎运行落库后填充"
        style={{ padding: "16px 0" }}
      />
    );
  }
  return (
    <Flex vertical gap={6}>
      {rows.map((r) => {
        const m = meta[r.key] ?? { color: "default", text: r.key };
        return (
          <Flex key={r.key} align="center" justify="space-between" gap={8}>
            <Tag color={m.color} style={{ marginInlineEnd: 0 }}>{m.text}</Tag>
            <Flex align="center" gap={8} style={{ flex: 1 }}>
              <div
                style={{
                  height: 8,
                  borderRadius: 4,
                  background: "rgba(0,0,0,0.06)",
                  flex: 1,
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    height: "100%",
                    width: total > 0 ? `${(r.n / total) * 100}%` : "0%",
                    background: "var(--primary,#1677ff)",
                  }}
                />
              </div>
              <Typography.Text type="secondary" style={{ fontSize: 12, width: 30, textAlign: "right" }}>
                {r.n}
              </Typography.Text>
            </Flex>
          </Flex>
        );
      })}
    </Flex>
  );
}

import { Card, Empty, Flex, Skeleton, Statistic, Tooltip, Typography } from "antd";
import { InfoCircleOutlined } from "@ant-design/icons";
import type { StrategyMetrics } from "../../api/endpoints";
import { colorOfSign, pctText } from "../../styles/tokens";

interface Props {
  metrics: StrategyMetrics | null;
  loading: boolean;
}

function labelTip(label: string, tip: string) {
  return (
    <Tooltip title={tip}>
      <span>
        {label} <InfoCircleOutlined style={{ fontSize: 11, color: "rgba(0,0,0,0.35)" }} />
      </span>
    </Tooltip>
  );
}

const valueStyle = { fontSize: 22, fontWeight: 600, color: "rgba(0,0,0,0.88)" } as const;

export default function MetricSummaryCard({ metrics, loading }: Props) {
  const m = metrics;
  const cumPct = m?.cum_return_pct ?? 0;
  const mddPct = m?.max_drawdown_pct ?? 0;
  return (
    <Card
      size="small"
      style={{ marginBottom: 12 }}
      title="业绩指标 · 份额法"
      extra={
        m?.as_of ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            截至 {m.as_of}
          </Typography.Text>
        ) : undefined
      }
    >
      {loading ? (
        <Skeleton active paragraph={{ rows: 2 }} />
      ) : !m || m.settle_days === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="尚无结算样本——EOD 结算产出日报后，曲线与指标在此呈现（spec-01 §6.1）"
          style={{ padding: "8px 0" }}
        />
      ) : (
        <>
          <Flex gap={48} wrap align="flex-start" style={{ marginBottom: 8 }}>
            <Statistic
              title={labelTip("累计收益率", "份额法：∑份额盈亏/期初 NAV，逐日结算复利（spec-01 §6.1）")}
              value={cumPct}
              precision={2}
              suffix="%"
              valueStyle={{ color: colorOfSign(cumPct), fontSize: 26 }}
            />
            <Statistic
              title={labelTip("最大回撤", "区间内 NAV 峰值到谷值最大跌幅（正数表示回撤幅度）")}
              value={mddPct}
              precision={2}
              suffix="%"
              valueStyle={{ color: "rgba(0,0,0,0.88)", fontSize: 26 }}
            />
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {labelTip("信号胜率", m?.signal?.note ?? "主账户信号注册表已结清信号（spec-01 §8）")}
              </Typography.Text>
              <div style={{ fontSize: 26, fontWeight: 600, color: colorOfSign((m.signal.win_rate_pct ?? 50) - 50) }}>
                {m.signal.win_rate_pct != null ? `${m.signal.win_rate_pct.toFixed(1)}%` : "—"}
              </div>
            </div>
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>最新 NAV</Typography.Text>
              <div style={{ ...valueStyle, fontSize: 22 }}>{m.nav_last?.toFixed(4) ?? "—"}</div>
            </div>
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>结算交易日</Typography.Text>
              <div style={{ ...valueStyle, fontSize: 22 }}>{m.settle_days}</div>
            </div>
          </Flex>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            信号样本：{m.signal.done} 条已结清（胜 {m.signal.win_n} / 平 {m.signal.tie_n} / 负{" "}
            {m.signal.early_n}），了结均前向收益 {pctText(m.signal.avg_fwd_return_pct, false)}。
            指标口径：份额法 + 信号注册表（spec-06 §6.4 P2，口径标注 B4）。
          </Typography.Text>
          {m.exit && m.exit.done > 0 && (
            <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 4 }}>
              卖出决策（exit_trackings）：{m.exit.done} 笔（卖对 {m.exit.win_n} / 卖平{" "}
              {m.exit.tie_n} / 卖早 {m.exit.early_n}），均前向收益{" "}
              {pctText(m.exit.avg_fwd_return_pct, false)}，相对沪深300 超额{" "}
              {pctText(m.exit.avg_excess_pct)}。
            </Typography.Text>
          )}
        </>
      )}
    </Card>
  );
}

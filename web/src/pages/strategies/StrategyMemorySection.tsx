import { Flex, Skeleton, Tag, Tooltip, Typography } from "antd";
import { useMemo } from "react";
import type {
  StrategyMemoryList,
  ValidationWindow,
  ValidationWindowList,
} from "../../api/endpoints";
import { fmtBeijing } from "../../utils/time";
import { colorOfSign, pctText } from "../../styles/tokens";

interface Props {
  memory: StrategyMemoryList | null;
  windows: ValidationWindowList | null;
  loading?: boolean;
  maxItems?: number;
}

const DECISION_META: Record<string, { color: string; text: string }> = {
  activate: { color: "green", text: "晋升现役" },
  rollback: { color: "red", text: "否决候选" },
  sealed: { color: "default", text: "封存留证" },
};

function VerdictEvidence({ w }: { w: ValidationWindow }) {
  if (w.status === "in_progress") {
    return (
      <Flex align="center" gap={8} wrap style={{ marginTop: 2 }}>
        <Tag color="blue" style={{ marginInlineEnd: 0 }}>EVOQUANT 引擎验证中</Tag>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          窗口 {w.sessions_done}/{w.window_days} 会话 · 成交样本 {w.trade_samples}/{w.trade_target}
        </Typography.Text>
      </Flex>
    );
  }
  const m = DECISION_META[w.decision] ?? { color: "default", text: w.decision || "收口" };
  return (
    <Flex align="center" gap={8} wrap style={{ marginTop: 2 }}>
      <Tooltip title={w.decision_reason || undefined}>
        <Tag color={m.color} style={{ marginInlineEnd: 0 }}>EVOQUANT · {m.text}</Tag>
      </Tooltip>
      {w.expectation != null && (
        <Typography.Text
          type="secondary"
          style={{ fontSize: 12, color: colorOfSign(w.expectation) }}
        >
          期望值 {pctText(w.expectation)}
          {w.baseline_expectation != null && ` / 基线 ${pctText(w.baseline_expectation)}`}
          · 卖出样本 {w.trade_samples}
          {w.rule_violations > 0 || w.fuse_events > 0
            ? ` · 违规 ${w.rule_violations} · 熔断 ${w.fuse_events}`
            : " · 零违规"}
        </Typography.Text>
      )}
    </Flex>
  );
}

export default function StrategyMemorySection({ memory, windows, loading, maxItems }: Props) {
  const winByVer = useMemo(() => {
    const m = new Map<string, ValidationWindow>();
    (windows?.items ?? []).forEach((w) => m.set(w.version_no, w));
    return m;
  }, [windows]);

  if (loading && !memory) {
    return <Skeleton active paragraph={{ rows: 3 }} />;
  }
  if (!memory || memory.total === 0) {
    return (
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12, marginBottom: 0 }}>
        演进记忆（spec-02 §3.1 memory_entries type=strategy）尚无落库——引擎 EVOQUANT 优化流写入后
        逐条挂 version_no（spec-05 §4.1）在此展示。
      </Typography.Paragraph>
    );
  }
  const items = maxItems ? memory.items.slice(0, maxItems) : memory.items;
  const truncated = maxItems != null && memory.items.length > maxItems;
  return (
    <Flex vertical gap={4} style={{ marginTop: 12 }}>
      <Flex align="center" wrap gap={8}>
        <Typography.Text strong style={{ fontSize: 13 }}>
          演进记忆 · type=strategy（{memory.total}）
        </Typography.Text>
        {memory.versions.map((v) => (
          <Tag key={v} color="purple">{v}</Tag>
        ))}
      </Flex>
      {items.map((m) => {
        const win = winByVer.get(m.version_no || "");
        return (
          <div
            key={m.id}
            style={{
              border: "1px solid rgba(0,0,0,0.08)",
              borderRadius: 6,
              padding: "6px 10px",
              background: m.quality === "flagged" ? "rgba(255,77,79,0.05)" : undefined,
            }}
          >
            <Flex wrap gap={8} align="center" style={{ marginBottom: 2 }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {fmtBeijing(m.ts)}
              </Typography.Text>
              {m.version_no ? <Tag color="purple">{m.version_no}</Tag> : null}
              {m.quality === "flagged" && <Tag color="red">flagged</Tag>}
            </Flex>
            <Typography.Paragraph style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 13 }}>
              {m.body}
            </Typography.Paragraph>
            {win && <VerdictEvidence w={win} />}
            {(m.source || m.ref_ids.length > 0) && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                来源 {m.source || "—"}
                {m.ref_ids.length > 0 ? ` · 引用 ${m.ref_ids.join("、")}` : ""}
              </Typography.Text>
            )}
          </div>
        );
      })}
      {truncated && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          共 {memory.total} 条，仅展示最近 {maxItems} 条——完整时间线见策略详情页演进卡。
        </Typography.Text>
      )}
    </Flex>
  );
}

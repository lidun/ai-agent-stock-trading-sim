import { Flex, Progress, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { ValidationWindow } from "../../api/endpoints";
import { fmtBeijing } from "../../utils/time";

interface Props {
  dataSource: ValidationWindow[];
  scrollY?: number;
}

function pct(v: number | null): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
}
/** A 股习惯：涨红跌绿（spec-06 §5.1） */
function pnlColor(v: number | null): string | undefined {
  if (v == null) return undefined;
  if (v > 0) return "#cf1322";
  if (v < 0) return "#389e0d";
  return undefined;
}

const DECISION_META: Record<string, { color: string; text: string }> = {
  activate: { color: "green", text: "activate 已晋升现役" },
  rollback: { color: "red", text: "rollback 否决候选" },
  sealed: { color: "default", text: "sealed 封存留证" },
};

function WindowTag({ w }: { w: ValidationWindow }) {
  if (w.status === "in_progress") {
    return <Tag color="blue">in_progress 验证中</Tag>;
  }
  const m = DECISION_META[w.decision] ?? { color: "default", text: w.decision || "判定" };
  return <Tag color={m.color}>{m.text}</Tag>;
}

function EvCell({ ev, base }: { ev: number | null; base: number | null }) {
  if (ev == null) return <Typography.Text type="secondary">—</Typography.Text>;
  return (
    <Tooltip
      title={
        base == null
          ? "候选期望值（卖出样本净盈利率均值 %，含双边费）"
          : `主账户同窗同期基线 ${pct(base)}；候选 ≥ 基线或 > 0 方可晋升`
      }
    >
      <span style={{ color: pnlColor(ev) }}>{pct(ev)}</span>
      {base != null && (
        <Typography.Text type="secondary" style={{ fontSize: 11, marginLeft: 4 }}>
          / {pct(base)}
        </Typography.Text>
      )}
    </Tooltip>
  );
}

function AcctView({ title, acct }: { title: string; acct?: Record<string, unknown> }) {
  if (!acct) return null;
  const Num = (k: string): number => Number(acct[k] ?? 0);
  const money = (k: string): string => {
    const n = Num(k);
    return n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };
  const rows: [string, string][] = [
    ["NAV", String(acct.nav ?? "—")],
    ["可用现金", money("cash")],
    ["累计盈亏", `${Num("total_pnl") > 0 ? "+" : ""}${money("total_pnl")}`],
    ["今日盈亏", `${Num("today_pnl") > 0 ? "+" : ""}${money("today_pnl")}`],
    ["账户状态", (acct.status as string) ?? ""],
    ["生效版本", (acct.active_version_no as string) || "—"],
  ];
  return (
    <Flex vertical gap={2} style={{ flex: 1, minWidth: 220 }}>
      <Typography.Text strong style={{ fontSize: 12 }}>{title}</Typography.Text>
      {rows.map(([k, v]) => (
        <Flex key={k} justify="space-between" gap={8}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>{k}</Typography.Text>
          <Typography.Text
            style={{
              fontSize: 12,
              color: k === "累计盈亏" || k === "今日盈亏" ? pnlColor(Num(k === "累计盈亏" ? "total_pnl" : "today_pnl")) : undefined,
            }}
          >
            {v}
          </Typography.Text>
        </Flex>
      ))}
    </Flex>
  );
}

const columns: ColumnsType<ValidationWindow> = [
  {
    title: "候选版本", key: "version", width: 120,
    render: (_, w) => (
      <Flex vertical gap={0}>
        <Typography.Text strong>{w.version_no}</Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
          {w.status === "in_progress" ? `目标 ${w.window_days} 会话 / ${w.trade_target} 成交` : w.window_start_trade_date || "—"}
        </Typography.Text>
      </Flex>
    ),
  },
  { title: "窗口状态", key: "status", width: 160, render: (_, w) => <WindowTag w={w} /> },
  {
    title: "推进", key: "progress", width: 200, align: "right",
    render: (_, w) => (
      <Flex vertical gap={2} align="end">
        <span>
          会话 {w.sessions_done}/{w.window_days}
        </span>
        <Progress
          percent={w.window_days ? Math.min(100, Math.round((w.sessions_done / w.window_days) * 100)) : 0}
          size="small"
          showInfo={false}
          strokeColor="#1677ff"
          style={{ width: 132, margin: 0 }}
        />
        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
          成交样本 {w.trade_samples}/{w.trade_target}
        </Typography.Text>
        <Progress
          percent={w.trade_target ? Math.min(100, Math.round((w.trade_samples / w.trade_target) * 100)) : 0}
          size="small"
          showInfo={false}
          strokeColor={w.trade_samples >= w.trade_target ? "#52c41a" : "#722ed1"}
          style={{ width: 132, margin: 0 }}
        />
      </Flex>
    ),
  },
  {
    title: "风控留痕", key: "risk", width: 120, align: "center",
    render: (_, w) =>
      w.rule_violations || w.fuse_events ? (
        <Tag color="red">
          违规 {w.rule_violations} · 熔断 {w.fuse_events}
        </Tag>
      ) : (
        <Typography.Text type="secondary">0 / 0</Typography.Text>
      ),
  },
  {
    title: "期望值 EV / 基线", key: "ev", width: 170, align: "right",
    render: (_, w) => <EvCell ev={w.expectation} base={w.baseline_expectation} />,
  },
  {
    title: "判定结论", key: "decide", minWidth: 240,
    render: (_, w) =>
      w.status === "in_progress" ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          未到期——满窗或达样本阈值后由 EOD 引擎收口
        </Typography.Text>
      ) : (
        <Flex vertical gap={2}>
          <Typography.Text ellipsis style={{ fontSize: 12 }} title={w.decision_reason || undefined}>
            {w.decision_reason || "（未记录理由）"}
          </Typography.Text>
          {w.decided_ts && (
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              收口 {fmtBeijing(w.decided_ts)}
            </Typography.Text>
          )}
        </Flex>
      ),
  },
  {
    title: "创建", key: "created", width: 150,
    render: (_, w) => <Typography.Text type="secondary" style={{ fontSize: 12 }}>{fmtBeijing(w.created_ts)}</Typography.Text>,
  },
];

export default function ValidationWindowsTable({ dataSource, scrollY }: Props) {
  return (
    <Table<ValidationWindow>
      rowKey="id"
      size="small"
      columns={columns}
      dataSource={dataSource}
      pagination={false}
      scroll={{ x: 1080, ...(scrollY ? { y: scrollY } : {}) }}
      expandable={{
        expandedRowRender: (w) => {
          const f = w.final_snapshot?.final;
          return (
            <Flex gap={16} wrap>
              {f ? (
                <Flex vertical gap={2} style={{ flex: 1, minWidth: 240 }}>
                  <Typography.Text strong style={{ fontSize: 12 }}>
                    收口结论 · {f.decision}
                  </Typography.Text>
                  <Typography.Text style={{ fontSize: 12 }}>{f.reason}</Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    窗口 {f.window_start} → {f.last_advance} · 会话 {f.sessions_done} · 成交 {f.trade_samples}
                    {f.validation_ev_n != null && ` · 候选卖出样本 ${f.validation_ev_n}`}
                    {f.baseline_ev_n != null && ` · 基线样本 ${f.baseline_ev_n}`}
                  </Typography.Text>
                </Flex>
              ) : (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  窗口未收口，无判定快照。
                </Typography.Text>
              )}
              <AcctView title="验证账户（独立试运行）" acct={w.final_snapshot?.validation_account} />
              <AcctView title="主账户（现役基线）" acct={w.final_snapshot?.main_account} />
            </Flex>
          );
        },
      }}
    />
  );
}

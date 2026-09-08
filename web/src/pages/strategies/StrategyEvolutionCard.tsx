import { Alert, Card, Empty, Flex, Skeleton, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { EvolutionLedger, StrategyEvolution } from "../../api/endpoints";
import { fmtBeijing } from "../../utils/time";
import { colorOfSign, pctText } from "../../styles/tokens";

interface Props {
  evolution: StrategyEvolution | null;
  loading: boolean;
}

const ROLE_TAG: Record<string, { color: string; text: string }> = {
  main: { color: "blue", text: "现役" },
  trial: { color: "purple", text: "试运行" },
  validation: { color: "cyan", text: "验证" },
};

function AccountRoleTag({ role }: { role: string }) {
  const m = ROLE_TAG[role] ?? { color: "default", text: role };
  return <Tag color={m.color}>{m.text}</Tag>;
}

const ledgerColumns: ColumnsType<EvolutionLedger> = [
  {
    title: "账本",
    dataIndex: "label",
    width: 170,
    ellipsis: true,
    render: (v: string, r) => (
      <Tooltip title={r.account_id}>
        <span>{v}</span>
      </Tooltip>
    ),
  },
  { title: "角色", dataIndex: "role", width: 90, render: (r: string) => <AccountRoleTag role={r} /> },
  {
    title: "状态", dataIndex: "status", width: 100,
    render: (s: string) => {
      const color = s === "normal" ? "green" : s === "archived" ? "default" : "orange";
      const text = s === "normal" ? "正常" : s === "archived" ? "已归档" : s === "trial" ? "试运行中" : s;
      return <Tag color={color}>{text}</Tag>;
    },
  },
  { title: "版本", dataIndex: "active_version_no", width: 90 },
  { title: "区间", width: 200, render: (_, r) => (r.first_report_date ? `${r.first_report_date} ~ ${r.last_report_date}` : "—") },
  { title: "结算日", dataIndex: "report_days", width: 80, align: "right" },
  {
    title: "累计收益", dataIndex: "return_pct", width: 110, align: "right",
    render: (v: number | null) =>
      v == null ? (
        <Typography.Text type="secondary">—</Typography.Text>
      ) : (
        <span style={{ color: colorOfSign(v) }}>{pctText(v)}</span>
      ),
  },
  {
    title: "验证窗口", key: "trial", width: 130,
    render: (_, r) =>
      r.trial ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {r.trial.window_days} 日 · {r.trial.replay_status === "ok" ? "回放完成" : r.trial.replay_status}
        </Typography.Text>
      ) : (
        <Typography.Text type="secondary">—</Typography.Text>
      ),
  },
  { title: "建立", dataIndex: "created_ts", width: 150, render: (v: string) => fmtBeijing(v) },
];

export default function StrategyEvolutionCard({ evolution, loading }: Props) {
  const archive = evolution?.archive;
  return (
    <Card size="small" title="策略演进 · 账本与验收链">
      {loading ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : !evolution || evolution.ledgers.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="尚无策略账本——试运行/验证账户随 Agent 演进自动创建（spec-02 §9 多代版本链 v2 前以账户角色账本呈现）"
          style={{ padding: "16px 0" }}
        />
      ) : (
        <>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
            {evolution.note}
          </Typography.Paragraph>
          <Table<EvolutionLedger>
            rowKey="account_id"
            size="small"
            columns={ledgerColumns}
            dataSource={evolution.ledgers}
            pagination={false}
            scroll={{ x: 1120 }}
          />
          {archive && (
            <Flex vertical gap={4} style={{ marginTop: 12 }}>
              <Alert
                type={archive.decision === "launch" ? "success" : "error"}
                showIcon
                message={
                  <span>
                    验收结论（{fmtBeijing(archive.archived_ts)}）：{" "}
                    <b>{archive.decision === "launch" ? "通过 · 可上线" : "否决 · 不进入实盘"}</b>
                  </span>
                }
                description={
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {archive.verdict || "（未记录原因）"} —— 终 nav {archive.end_nav?.toFixed(4) ?? "—"}，累计盈亏{" "}
                    {archive.end_total_pnl?.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) ?? "—"} 元；回放{" "}
                    {archive.replay_window_days ?? "—"} 日 / {archive.replay_sessions ?? "—"} 场，结算{" "}
                    {archive.settle_days ?? "—"} 日，成交流水 {archive.trades ?? "—"} 笔（条件单 {archive.orders ?? "—"} /{" "}
                    期末持仓 {archive.holdings ?? "—"}）。
                  </Typography.Text>
                }
              />
            </Flex>
          )}
        </>
      )}
    </Card>
  );
}

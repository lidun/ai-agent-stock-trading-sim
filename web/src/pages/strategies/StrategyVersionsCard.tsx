import { Card, Empty, Flex, Skeleton, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { StrategyVersion, StrategyVersionList } from "../../api/endpoints";
import { fmtBeijing } from "../../utils/time";

interface Props {
  versions: StrategyVersionList | null;
  loading: boolean;
}

const STATUS_META: Record<string, { color: string; text: string }> = {
  active: { color: "green", text: "active 现役" },
  validated: { color: "cyan", text: "validated 候选" },
  draft: { color: "gold", text: "draft 验证中" },
  rolled_back: { color: "red", text: "rolled_back" },
};

function StatusTag({ status }: { status: string }) {
  const m = STATUS_META[status] ?? { color: "default", text: status };
  return <Tag color={m.color}>{m.text}</Tag>;
}

function DiffPre({ title, value }: { title: string; value: Record<string, unknown> }) {
  const keys = Object.keys(value ?? {});
  if (keys.length === 0) return null;
  return (
    <div>
      <Typography.Text strong style={{ fontSize: 12 }}>{title}</Typography.Text>
      <pre
        style={{
          margin: "2px 0 0",
          fontSize: 12,
          whiteSpace: "pre-wrap",
          background: "rgba(0,0,0,0.03)",
          borderRadius: 6,
          padding: "6px 8px",
        }}
      >
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

const columns: ColumnsType<StrategyVersion> = [
  {
    title: "版本", key: "version", width: 130,
    render: (_, v) => (
      <Flex vertical gap={0}>
        <Typography.Text strong>{v.version_no}</Typography.Text>
        {v.parent_version ? (
          <Typography.Text type="secondary" style={{ fontSize: 11 }}>
            parent {v.parent_version}
          </Typography.Text>
        ) : (
          <Typography.Text type="secondary" style={{ fontSize: 11 }}>首版</Typography.Text>
        )}
      </Flex>
    ),
  },
  { title: "状态", dataIndex: "status", width: 150, render: (s: string) => <StatusTag status={s} /> },
  {
    title: "依据/验证", key: "meta", minWidth: 260,
    render: (_, v) => (
      <Flex vertical gap={3}>
        {v.basis.length > 0 ? (
          <span>
            {v.basis.map((b) => <Tag key={b} style={{ marginInlineEnd: 4 }}>{b}</Tag>)}
          </span>
        ) : (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>无诊断依据引用</Typography.Text>
        )}
        {(v.trial_window && Object.keys(v.trial_window).length > 0) && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            验证窗 {(v.trial_window.window_days as number | undefined) ?? "—"} 日 · 仓位上限{" "}
            {(v.trial_window.cap_ceiling ?? 1) as number * 100}%
          </Typography.Text>
        )}
      </Flex>
    ),
  },
  {
    title: "关键时点", key: "dates", width: 200,
    render: (_, v) => (
      <Flex vertical gap={2}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          建 {fmtBeijing(v.created_ts)} · {v.created_by === "manager" ? "管理 Agent" : "策略 Agent"}
        </Typography.Text>
        {v.validated_on && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>晋升 {fmtBeijing(v.validated_on)}</Typography.Text>
        )}
      </Flex>
    ),
  },
  {
    title: "失败/回退", key: "fail", width: 220,
    render: (_, v) =>
      v.status === "rolled_back" ? (
        <Flex vertical gap={2}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {v.failure_reason ? `原因：${v.failure_reason}` : "（未记录原因）"}
          </Typography.Text>
          {v.rolled_back_to && <Tag color="geekblue">回退至 {v.rolled_back_to}</Tag>}
        </Flex>
      ) : (
        <Typography.Text type="secondary">—</Typography.Text>
      ),
  },
];

export default function StrategyVersionsCard({ versions, loading }: Props) {
  return (
    <Card
      size="small"
      title="策略版本状态机 · config 快照（spec-02 §9 / EVOQUANT）"
      extra={
        versions && (
          <Tag color="green" style={{ marginInlineEnd: 0 }}>
            现役 {versions.active_version || "—"}
          </Tag>
        )
      }
    >
      {loading ? (
        <Skeleton active paragraph={{ rows: 4 }} />
      ) : !versions || versions.total === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="尚无引擎版本——EVOQUANT 优化流 checkpoint 后在此呈现 config 快照与 晋升/回滚 状态（spec-05 §4.1）"
          style={{ padding: "12px 0" }}
        />
      ) : (
        <>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
            每 Agent 至多一个 active（部分唯一索引兜底）；回退需最近 validated 候选，否则拒绝执行并转管理 Agent 待办。
          </Typography.Paragraph>
          <Table<StrategyVersion>
            rowKey="id"
            size="small"
            columns={columns}
            dataSource={versions.items}
            pagination={false}
            scroll={{ x: 980 }}
            expandable={{
              expandedRowRender: (v) => (
                <Flex gap={16} wrap>
                  <DiffPre title="config 全量快照" value={v.config} />
                  <DiffPre title="config_diff（相对父版本）" value={v.config_diff} />
                </Flex>
              ),
            }}
          />
        </>
      )}
    </Card>
  );
}

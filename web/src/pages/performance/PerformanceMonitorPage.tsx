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
  fetchPerformanceSnapshot,
  type PerformanceSnapshot,
} from "../../api/endpoints";
import { fmtBeijingTime } from "../../utils/time";

const MSG_STATUS_META: Record<string, { color: string; text: string }> = {
  queued: { color: "gold", text: "queued 排队" },
  processing: { color: "blue", text: "processing 处理中" },
  delivered: { color: "green", text: "delivered 已送达" },
  pending_review: { color: "purple", text: "pending_review" },
  failed: { color: "red", text: "failed 失败" },
};

const APPROVAL_STATUS_META: Record<string, { color: string; text: string }> = {
  pending: { color: "orange", text: "待决" },
  approved: { color: "green", text: "已批准" },
  rejected: { color: "red", text: "已驳回" },
  expired: { color: "default", text: "已过期" },
  withdrawn: { color: "default", text: "已撤回" },
};

function fmtDuration(s: number): string {
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  const h = Math.floor(s / 3600);
  return `${h} 时 ${Math.floor((s % 3600) / 60)} 分`;
}

function statusRows(dist: Record<string, number>) {
  return Object.entries(dist)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => ({ key: k, status: k, n }));
}

const staleColumns: ColumnsType<PerformanceSnapshot["tasks"]["stale_active"][number]> = [
  { title: "Agent", dataIndex: "agent_id", width: 170 },
  {
    title: "状态", dataIndex: "status", width: 130,
    render: (s: string) => {
      const m = MSG_STATUS_META[s] ?? { color: "default", text: s };
      return <Tag color={m.color}>{m.text}</Tag>;
    },
  },
  {
    title: "滞留时长", dataIndex: "age_s", width: 120,
    render: (v: number) => <Typography.Text type={v > 300 ? "danger" : undefined}>{fmtDuration(v)}</Typography.Text>,
  },
  {
    title: "请求片段", dataIndex: "body_preview", ellipsis: true,
    render: (v: string) => v || "—",
  },
];

export default function PerformanceMonitorPage() {
  const [data, setData] = useState<PerformanceSnapshot | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setData(await fetchPerformanceSnapshot());
    } catch (e) {
      console.error("performance snapshot failed", e);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
    const t = window.setInterval(() => void reload(), 15000);
    return () => window.clearInterval(t);
  }, [reload]);

  const live = data?.tasks.live ?? 0;
  const stale = data?.tasks.stale_active ?? [];
  const apNext = data?.approvals.next_expires_in_s;

  return (
    <div style={{ padding: 16, minHeight: "100%" }}>
      <Flex justify="space-between" align="flex-start" style={{ marginBottom: 12 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            性能监控
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            现状快照（spec-04 §9 / spec-06 §6.1）：回执链滞留 / 1h 流转 / 审批倒计时 / 进程态；
            每 15s 自动刷新，时序曲线无独立埋点故不绘伪图
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
          <Empty description="快照不可用——后端只读接口返回异常" style={{ padding: "40px 0" }} />
        </Card>
      ) : (
        <Flex vertical gap={12}>
          <Flex gap={12} wrap>
            <Card size="small" style={{ flex: "1 1 180px" }}>
              <Statistic title="进程运行时长" value={fmtDuration(data.uptime_s)} valueStyle={{ fontSize: 18 }} />
            </Card>
            <Card size="small" style={{ flex: "1 1 180px" }}>
              <Statistic title="活跃回执任务" value={live}
                suffix={live > 0 ? <Tag color="gold">滞留中</Tag> : null}
                valueStyle={{ fontSize: 22 }}
              />
            </Card>
            <Card size="small" style={{ flex: "1 1 180px" }}>
              <Statistic title="近 1h 送达" value={data.msg_1h.delivered}
                suffix={<Typography.Text type="secondary" style={{ fontSize: 12 }}>条</Typography.Text>}
              />
            </Card>
            <Card size="small" style={{ flex: "1 1 180px" }}>
              <Statistic title="待决审批" value={data.approvals.pending}
                suffix={apNext !== null && apNext !== undefined
                  ? <Tag color="orange" style={{ marginLeft: 4 }}>最快 {fmtDuration(apNext)} 过期</Tag>
                  : null}
              />
            </Card>
            <Card size="small" style={{ flex: "1 1 220px" }}>
              <Statistic title="最近结算新鲜度"
                value={data.last_settle
                  ? `${data.last_settle.trade_date} · ${fmtDuration(data.last_settle.fresh_s)}前`
                  : "无运行"}
                valueStyle={{ fontSize: 14 }}
              />
            </Card>
          </Flex>

          <Flex gap={12} wrap align="stretch">
            <Card size="small" title="回执链任务分布（messages.status）" style={{ flex: "1 1 360px", minWidth: 330 }}>
              {data.tasks.total === 0 ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无消息回执行" style={{ padding: "12px 0" }} />
              ) : (
                <Space size={[4, 8]} wrap>
                  {statusRows(data.tasks.status).map((r) => {
                    const m = MSG_STATUS_META[r.status] ?? { color: "default", text: r.status };
                    return <Tag key={r.status} color={m.color}>{m.text}：{r.n}</Tag>;
                  })}
                </Space>
              )}
            </Card>

            <Card size="small" title="审批状态分布" style={{ flex: "1 1 360px", minWidth: 330 }}>
              {Object.keys(data.approvals.status).length === 0 ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无审批留痕" style={{ padding: "12px 0" }} />
              ) : (
                <Space size={[4, 8]} wrap>
                  {statusRows(data.approvals.status).map((r) => {
                    const m = APPROVAL_STATUS_META[r.status] ?? { color: "default", text: r.status };
                    return <Tag key={r.status} color={m.color}>{m.text}：{r.n}</Tag>;
                  })}
                </Space>
              )}
            </Card>
          </Flex>

          <Card
            size="small"
            title="活跃滞留任务（queued/processing 超过常驻窗口即异常）"
            extra={
              <Space size={4}>
                <Tag>进程内 ws 在线 {data.process.ws_clients}</Tag>
                <Tag>引擎桩延迟 {data.process.engine_stub_delay_ms}ms</Tag>
              </Space>
            }
          >
            {stale.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无滞留任务——回执链即时推进" style={{ padding: "12px 0" }} />
            ) : (
              <Table
                rowKey="id"
                size="small"
                columns={staleColumns}
                dataSource={stale}
                pagination={false}
                scroll={{ x: 640 }}
              />
            )}
          </Card>

          <Alert type="info" showIcon message={data.msg_1h.scope} />
          <Typography.Text type="secondary" style={{ fontSize: 11, alignSelf: "flex-end" }}>
            快照于 {fmtBeijingTime(data.snapshot_ts)}
          </Typography.Text>
        </Flex>
      )}
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Empty,
  Flex,
  Progress,
  Skeleton,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined, PlayCircleOutlined } from "@ant-design/icons";
import {
  fetchPerformanceSnapshot,
  fetchSchedulerStatus,
  fetchUsageDaily,
  fetchUsageGroup,
  runSchedulerTick,
  type PerformanceSnapshot,
  type SchedulerStatus,
  type UsageDailyRow,
  type UsageGroupRow,
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

function costCell(cost: number | null) {
  return cost === null || cost === undefined
    ? <Tag>未计价</Tag>
    : <Typography.Text>{cost.toFixed(4)} 元</Typography.Text>;
}

const usageDailyColumns: ColumnsType<UsageDailyRow> = [
  { title: "日期", dataIndex: "date", width: 120 },
  { title: "调用", dataIndex: "llm_calls", width: 70, align: "right" },
  { title: "tokens in", dataIndex: "tokens_in", width: 90, align: "right" },
  { title: "其中缓存命中", dataIndex: "cached_tokens", width: 110, align: "right" },
  { title: "tokens out", dataIndex: "tokens_out", width: 90, align: "right" },
  {
    title: "费用", dataIndex: "cost_yuan", width: 110, align: "right",
    render: (v: number | null) => costCell(v),
  },
];

const usageGroupColumns: ColumnsType<UsageGroupRow> = [
  { title: "Agent", dataIndex: "agent_id", width: 170, ellipsis: true },
  { title: "任务类型", dataIndex: "task_type", width: 110 },
  { title: "调用", dataIndex: "llm_calls", width: 70, align: "right" },
  { title: "tokens in", dataIndex: "tokens_in", width: 90, align: "right" },
  { title: "tokens out", dataIndex: "tokens_out", width: 90, align: "right" },
  {
    title: "费用", dataIndex: "cost_yuan", width: 110, align: "right",
    render: (v: number | null) => costCell(v),
  },
];

function sumCost(rows: { cost_yuan: number | null }[]): number | null {
  let total: number | null = null;
  let allPriced = true;
  for (const r of rows) {
    if (r.cost_yuan === null || r.cost_yuan === undefined) {
      allPriced = false;
      continue;
    }
    total = (total ?? 0) + r.cost_yuan;
  }
  if (!allPriced && total === null) return null;
  return total === null ? null : Number(total.toFixed(4));
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
  const { message } = AntApp.useApp();
  const [data, setData] = useState<PerformanceSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null);
  const [ticking, setTicking] = useState(false);
  const [usage, setUsage] = useState<{
    daily: UsageDailyRow[];
    group: UsageGroupRow[];
  } | null>(null);

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
    try {
      setScheduler(await fetchSchedulerStatus());
    } catch (e) {
      console.error("scheduler status failed", e);
      setScheduler(null);
    }
    try {
      const [daily, group] = await Promise.all([
        fetchUsageDaily(14),
        fetchUsageGroup(14),
      ]);
      setUsage({ daily: daily.days, group: group.rows });
    } catch (e) {
      console.error("usage summary failed", e);
    }
  }, []);

  const onTick = useCallback(async () => {
    setTicking(true);
    try {
      const r = await runSchedulerTick();
      const ran = r.deferrable;
      message.success(
        `tick 完成：崩溃恢复 ${r.recovered_tasks} 项，` +
          `清扫过期审批 ${r.expired_approvals} 项，` +
          `interrupt 超时 ${r.interrupt_timed_out} 项，` +
          `可延迟任务 认领 ${ran.claimed} / 完成 ${ran.done} / 失败 ${ran.failed} / 跳过 ${ran.skipped}` +
          (ran.deferred ? ` / 降级延后 ${ran.deferred}` : ""),
      );
      await reload();
    } catch (e) {
      console.error("scheduler tick failed", e);
      message.error("tick 失败——后端拒绝或不可达");
    } finally {
      setTicking(false);
    }
  }, [message, reload]);

  useEffect(() => {
    void reload();
    const t = window.setInterval(() => void reload(), 15000);
    return () => window.clearInterval(t);
  }, [reload]);

  const live = data?.tasks.live ?? 0;
  const stale = data?.tasks.stale_active ?? [];
  const apNext = data?.approvals.next_expires_in_s;
  const uDaily = usage?.daily ?? [];
  const uTotalCost = sumCost(uDaily);
  const uTotalCalls = uDaily.reduce((n, r) => n + r.llm_calls, 0);
  const uTokensIn = uDaily.reduce((n, r) => n + r.tokens_in, 0);
  const uTokensOut = uDaily.reduce((n, r) => n + r.tokens_out, 0);
  const cap = scheduler?.capacity;
  const capPct = (ratio: number) => Math.round(Math.max(0, Math.min(1, ratio)) * 100);
  const slackOk = scheduler?.idle ?? false;

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

      <Card
        size="small"
        title="调度器 · 空闲窗口与资源闸门（spec-04 §2.5/§7.2）"
        style={{ marginTop: 12 }}
        extra={
          <Space size={4}>
            {scheduler ? (
              <Tag color={scheduler.manager_mode === "autonomous" ? "red" : "green"}>
                {scheduler.manager_mode === "autonomous" ? "管理 Agent 安全自治" : "管理 Agent 正常"}
              </Tag>
            ) : null}
            <Tag color={slackOk ? "green" : "default"}>
              {slackOk ? "空闲可跑" : "非空闲"}
            </Tag>
            <Button
              size="small"
              type="primary"
              icon={<PlayCircleOutlined />}
              loading={ticking}
              onClick={() => void onTick()}
            >
              立即 tick
            </Button>
          </Space>
        }
      >
        {!scheduler ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="调度器状态不可用"
            style={{ padding: "12px 0" }}
          />
        ) : (
          <Flex vertical gap={12}>
            <Flex gap={12} wrap>
              <Card size="small" style={{ flex: "1 1 160px" }}>
                <Statistic title="待办任务" value={scheduler.pending_tasks}
                  suffix={<Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    其中可延迟 {scheduler.pending_deferrable}
                  </Typography.Text>} />
              </Card>
              <Card size="small" style={{ flex: "1 1 160px" }}>
                <Statistic title="运行中任务" value={scheduler.running_tasks} />
              </Card>
              <Card size="small" style={{ flex: "1 1 160px" }}>
                <Statistic title="待决审批" value={scheduler.pending_approvals} />
              </Card>
              <Card size="small" style={{ flex: "1 1 160px" }}>
                <Statistic title="interrupt 挂起" value={scheduler.pending_interrupts}
                  suffix={scheduler.pending_interrupts > 0
                    ? <Tag color="orange" style={{ marginLeft: 4 }}>30min 超时保守拒绝</Tag>
                    : null} />
              </Card>
              <Card size="small" style={{ flex: "1.4 1 240px" }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  空闲判定
                </Typography.Text>
                <div style={{ marginTop: 4 }}>
                  <Typography.Text>{scheduler.idle_reason}</Typography.Text>
                </div>
              </Card>
            </Flex>
            <Flex gap={18} wrap align="center">
              <div style={{ flex: "1 1 220px", minWidth: 200 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  CPU 余量 {cap?.known ? `${capPct(cap.cpu_avail_ratio)}%（load1 ${cap.load1.toFixed(2)} / ${cap.cpu_count} 核）` : "采样不可用"}
                </Typography.Text>
                <Progress
                  percent={cap?.known ? capPct(cap.cpu_avail_ratio) : 0}
                  status={cap?.known && cap.cpu_avail_ratio < 0.30 ? "exception" : "normal"}
                  size="small"
                />
              </div>
              <div style={{ flex: "1 1 220px", minWidth: 200 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  内存余量 {cap?.known ? `${capPct(cap.mem_avail_ratio)}%` : "采样不可用"}
                </Typography.Text>
                <Progress
                  percent={cap?.known ? capPct(cap.mem_avail_ratio) : 0}
                  status={cap?.known && cap.mem_avail_ratio < 0.30 ? "exception" : "normal"}
                  size="small"
                />
              </div>
              <Tag color="blue">新任务准入阈值 30%</Tag>
              <Tag color="blue">任务内 LLM 调用阈值 10%</Tag>
            </Flex>
            <Alert
              type="info"
              showIcon
              message="tick 仅在空闲窗口执行可延迟任务（经验提取 / kb_stats 刷新）；手动触发用于本地验证，生产由空闲巡检驱动。"
            />
          </Flex>
        )}
      </Card>

      <Card
        size="small"
        title="费用台账（LLM 消费 · spec-02 §11 · 近 14 日）"
        style={{ marginTop: 12 }}
        extra={<Tag>单价未配置的记录只留 token 并标「未计价」，不伪造费用</Tag>}
      >
        {usage === null || uDaily.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="暂无 LLM 费用留痕——首次日报叙述段生成 / LLM 调用后出现"
            style={{ padding: "12px 0" }}
          />
        ) : (
          <Flex vertical gap={12}>
            <Flex gap={12} wrap>
              <Card size="small" style={{ flex: "1 1 160px" }}>
                <Statistic title="近 14 日调用" value={uTotalCalls} valueStyle={{ fontSize: 18 }} />
              </Card>
              <Card size="small" style={{ flex: "1 1 200px" }}>
                <Statistic
                  title="近 14 日费用"
                  value={uTotalCost === null ? "未计价" : `${uTotalCost.toFixed(4)} 元`}
                  valueStyle={{ fontSize: 18 }}
                />
              </Card>
              <Card size="small" style={{ flex: "1 1 160px" }}>
                <Statistic title="tokens in（含缓存）" value={uTokensIn} valueStyle={{ fontSize: 18 }} />
              </Card>
              <Card size="small" style={{ flex: "1 1 160px" }}>
                <Statistic title="tokens out" value={uTokensOut} valueStyle={{ fontSize: 18 }} />
              </Card>
            </Flex>
            <Flex gap={12} wrap align="stretch">
              <Card size="small" title="元 / 日" style={{ flex: "1 1 380px", minWidth: 340 }}>
                <Table
                  rowKey="date"
                  size="small"
                  columns={usageDailyColumns}
                  dataSource={uDaily}
                  pagination={false}
                  scroll={{ x: 520 }}
                />
              </Card>
              <Card size="small" title="费用归属（Agent × 任务类型）" style={{ flex: "1 1 380px", minWidth: 340 }}>
                <Table
                  rowKey={(r) => `${r.agent_id}:${r.task_type}`}
                  size="small"
                  columns={usageGroupColumns}
                  dataSource={usage?.group ?? []}
                  pagination={false}
                  scroll={{ x: 520 }}
                />
              </Card>
            </Flex>
          </Flex>
        )}
      </Card>
    </div>
  );
}

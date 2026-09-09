import { useEffect, useMemo, useState } from "react";
import {
  Card,
  Divider,
  Drawer,
  Empty,
  Flex,
  Skeleton,
  Statistic,
  Table,
  Tag,
  Typography,
  theme as antTheme,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { CrownOutlined, RobotOutlined } from "@ant-design/icons";
import {
  fetchAgentCapabilityBindings,
  fetchStrategyProfile,
  fetchValidationWindows,
  type AgentCapabilityBindings,
  type AgentInfo,
  type CapabilityBindingItem,
  type StrategyProfile,
  type ValidationWindow,
  type ValidationWindowList,
} from "../../api/endpoints";
import { fmtBeijingTime } from "../../utils/time";

interface Props {
  agent: AgentInfo | null;
  onClose: () => void;
}

const TYPE_COLOR: Record<string, string> = {
  skill: "geekblue",
  tool: "purple",
  mcp: "magenta",
  datasource: "cyan",
};

const ROLE_COLOR: Record<string, string> = {
  manager: "#722ed1",
  strategy: "#13c2c2",
};

function StateHint({ profile }: { profile: StrategyProfile | null }) {
  const versionRows = profile?.versions ?? [];
  if (!profile) return null;
  if (versionRows.length === 0) {
    return (
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        尚无章程落库——理念由发布/管理 Agent 写入后在此可见（spec-05 §4.1 双层结构）
      </Typography.Text>
    );
  }
  return null;
}

function BoundStatus({ item }: { item: CapabilityBindingItem }) {
  if (item.unbound_ts) return <Tag color="default">已解绑 {fmtBeijingTime(item.unbound_ts)}</Tag>;
  const meta =
    item.capability_status === "deprecated"
      ? { color: "red", text: "能力已停用(deprecated)" }
      : { color: "green", text: "已下发" };
  return <Tag color={meta.color}>{meta.text}</Tag>;
}

const WIN_DECISION: Record<string, { color: string; text: string }> = {
  activate: { color: "green", text: "activate 晋升" },
  rollback: { color: "red", text: "rollback 否决" },
  sealed: { color: "default", text: "sealed 封存" },
};

function pct(v: number | null): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
}

const winColumns: ColumnsType<ValidationWindow> = [
  {
    title: "候选版本", key: "v", width: 90,
    render: (_, w) => <Typography.Text code style={{ fontSize: 12 }}>{w.version_no}</Typography.Text>,
  },
  {
    title: "状态", key: "st", width: 150,
    render: (_, w) =>
      w.status === "in_progress" ? (
        <Tag color="blue">in_progress 验证中</Tag>
      ) : (
        (() => {
          const m = WIN_DECISION[w.decision] ?? { color: "default", text: w.decision || "已收口" };
          return <Tag color={m.color}>{m.text}</Tag>;
        })()
      ),
  },
  {
    title: "推进", key: "p", width: 150, align: "right",
    render: (_, w) => (
      <Flex vertical gap={0} align="flex-end">
        <Typography.Text style={{ fontSize: 12 }}>
          会话 {w.sessions_done}/{w.window_days}
        </Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
          成交样本 {w.trade_samples}/{w.trade_target}
        </Typography.Text>
      </Flex>
    ),
  },
  {
    title: "期望值 EV / 基线", key: "ev", width: 150, align: "right",
    render: (_, w) => {
      if (w.expectation == null) return <Typography.Text type="secondary">—</Typography.Text>;
      return (
        <Flex vertical gap={0} align="flex-end">
          <Typography.Text
            style={{ fontSize: 12, color: w.expectation > 0 ? "#cf1322" : w.expectation < 0 ? "#389e0d" : undefined }}
          >
            {pct(w.expectation)}
          </Typography.Text>
          {w.baseline_expectation != null && (
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              基线 {pct(w.baseline_expectation)}
            </Typography.Text>
          )}
        </Flex>
      );
    },
  },
  {
    title: "风控留痕", key: "risk", width: 90, align: "center",
    render: (_, w) =>
      w.rule_violations || w.fuse_events ? (
        <Tag color="red">违规{w.rule_violations}/熔断{w.fuse_events}</Tag>
      ) : (
        <Typography.Text type="secondary">0/0</Typography.Text>
      ),
  },
  {
    title: "判定结论", key: "d", width: 300,
    render: (_, w) =>
      w.status === "in_progress" ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>未到期</Typography.Text>
      ) : (
        <Typography.Text ellipsis style={{ fontSize: 12 }} title={w.decision_reason || undefined}>
          {w.decision_reason || "（未记录理由）"}
        </Typography.Text>
      ),
  },
  {
    title: "关键时点", key: "t", width: 180,
    render: (_, w) => (
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {w.status === "in_progress" ? "开窗 " : "收口 "}
        {fmtBeijingTime(w.status === "in_progress" ? w.created_ts : w.decided_ts)}
      </Typography.Text>
    ),
  },
];

export default function AgentOverviewDrawer({ agent, onClose }: Props) {
  const { token } = antTheme.useToken();
  const [profile, setProfile] = useState<StrategyProfile | null>(null);
  const [bindings, setBindings] = useState<AgentCapabilityBindings | null>(null);
  const [windows, setWindows] = useState<ValidationWindowList | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!agent) return;
    let live = true;
    setLoading(true);
    setProfile(null);
    setBindings(null);
    setWindows(null);
    Promise.allSettled([
      fetchStrategyProfile(agent.id),
      fetchAgentCapabilityBindings(agent.id, false),
      fetchValidationWindows(agent.id),
    ]).then(([p, b, w]) => {
      if (!live) return;
      setProfile(p.status === "fulfilled" ? p.value : null);
      setBindings(b.status === "fulfilled" ? b.value : null);
      setWindows(w.status === "fulfilled" ? w.value : null);
      setLoading(false);
    });
    return () => {
      live = false;
    };
  }, [agent]);

  const active = profile?.active ?? null;
  const versionRows = useMemo(
    () =>
      [...(profile?.versions ?? [])].sort((x, y) =>
        y.created_ts.localeCompare(x.created_ts),
      ),
    [profile],
  );
  const boundRows = useMemo(
    () => (bindings?.items ?? []).filter((x) => !x.unbound_ts),
    [bindings],
  );

  const capColumns: ColumnsType<CapabilityBindingItem> = [
    {
      title: "能力包", dataIndex: "name", width: 200,
      render: (v: string, r) => (
        <Flex vertical gap={2}>
          <Typography.Text strong>{v}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 11 }} code>
            {r.capability_id}
          </Typography.Text>
        </Flex>
      ),
    },
    {
      title: "类型/版本", dataIndex: "type", width: 150,
      render: (t: string, r) => (
        <Flex vertical gap={2}>
          <Tag color={TYPE_COLOR[t] ?? "default"} style={{ width: "fit-content", marginInlineEnd: 0 }}>
            {t}
          </Tag>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>v{r.version}</Typography.Text>
        </Flex>
      ),
    },
    {
      title: "下发状态", key: "status", width: 150,
      render: (_, r) => <BoundStatus item={r} />,
    },
    {
      title: "下发来源/时间", dataIndex: "bound_ts", width: 180,
      render: (ts: string, r) => (
        <Flex vertical gap={2}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {r.bound_by === "manager" ? "管理 Agent" : r.bound_by}
          </Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {fmtBeijingTime(ts)}
          </Typography.Text>
        </Flex>
      ),
    },
  ];

  const isManager = agent?.role === "manager";
  const charterLoading = loading && !profile;
  const capLoading = loading && !bindings;
  const winLoading = loading && !windows;
  const winRows = windows?.items ?? [];
  const winRunning = winRows.filter((w) => w.status === "in_progress").length;

  return (
    <Drawer
      title={
        agent && (
          <Flex align="center" gap={10}>
            <span
              style={{
                width: 22,
                height: 22,
                borderRadius: "50%",
                background: ROLE_COLOR[agent.role] ?? "#888",
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                color: "#fff",
                fontSize: 12,
              }}
            >
              {isManager ? <CrownOutlined /> : <RobotOutlined />}
            </span>
            <Typography.Text strong>{agent.name}</Typography.Text>
            <Tag color="default" style={{ marginInlineEnd: 0 }}>
              {isManager ? "管理 Agent" : "策略子 Agent"}
            </Tag>
          </Flex>
        )
      }
      width={980}
      open={agent !== null}
      onClose={onClose}
      footer={null}
    >
      {!agent ? null : (
        <>
          {isManager && (
            <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: "10px 14px" } }}>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                管理 Agent 承担需求 / 策略评估 / 审批与能力下发，本身不持有可执行层 config；下方展示它作为发布方对策略 Agent 的能力分发视图。
              </Typography.Text>
            </Card>
          )}
          <Flex gap={12} align="stretch" wrap={false}>
            <Card
              size="small"
              title="策略章程 · 版本链（理念层）"
              style={{ flex: "1 1 460px", minWidth: 420 }}
            >
              {charterLoading ? (
                <Skeleton active paragraph={{ rows: 3 }} />
              ) : profile === null ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="策略章程不可读（非策略域或尚无数据）" />
              ) : (
                <>
                  <StateHint profile={profile} />
                  {active && (
                    <>
                      <Flex gap={8} wrap align="center" style={{ marginBottom: 8 }}>
                        <Statistic
                          title="现役版本"
                          value={active.version_no}
                          valueStyle={{ fontSize: 22, fontWeight: 600 }}
                          style={{ marginRight: 8 }}
                        />
                        <Tag color="green">in effect</Tag>
                        <Tag color={active.locked ? "red" : "default"}>
                          {active.locked ? "理念锁定" : "理念未锁定"}
                        </Tag>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {fmtBeijingTime(active.created_ts)}
                        </Typography.Text>
                      </Flex>
                      <div
                        style={{
                          borderLeft: "3px solid #cf1322",
                          background: token.colorFillQuaternary,
                          padding: "6px 10px",
                          borderRadius: 4,
                          marginBottom: 6,
                        }}
                      >
                        <Typography.Paragraph
                          style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 13 }}
                        >
                          {active.core_belief || "（该版本未记录理念正文）"}
                        </Typography.Paragraph>
                      </div>
                      {active.note && (
                        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 6 }}>
                          变更说明：{active.note}
                        </Typography.Paragraph>
                      )}
                    </>
                  )}
                  <Divider style={{ margin: "8px 0" }} />
                  <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 6 }}>
                    版本链 {versionRows.length ? `${versionRows.length} 个版本` : ""}：
                  </Typography.Text>
                  {versionRows.length === 0 ? (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      （无）
                    </Typography.Text>
                  ) : (
                    <Flex vertical gap={4}>
                      {versionRows.map((v) => (
                        <Flex
                          key={v.version_no}
                          gap={8}
                          align="center"
                          justify="space-between"
                          style={{
                            padding: "4px 8px",
                            borderRadius: 4,
                            background:
                              v.version_no === active?.version_no
                                ? "rgba(82,196,26,0.10)"
                                : "transparent",
                          }}
                        >
                          <Flex gap={8} align="center" style={{ minWidth: 0 }}>
                            <Typography.Text code style={{ fontSize: 12 }}>
                              {v.version_no}
                            </Typography.Text>
                            {v.active ? <Tag color="blue">现役</Tag> : null}
                            {v.locked ? <Tag color="red">锁定</Tag> : null}
                          </Flex>
                          <Flex gap={8} align="center" style={{ minWidth: 0 }}>
                            {v.note && (
                              <Typography.Text
                                type="secondary"
                                ellipsis
                                style={{ fontSize: 12, maxWidth: 220 }}
                              >
                                {v.note}
                              </Typography.Text>
                            )}
                            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                              {fmtBeijingTime(v.created_ts)}
                            </Typography.Text>
                          </Flex>
                        </Flex>
                      ))}
                    </Flex>
                  )}
                  <Typography.Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 8 }}>
                    “理念锁定变更需用户授权” 语义见 spec-05 §4.1；能力域改走右侧下发清单。
                  </Typography.Text>
                </>
              )}
            </Card>

            <Card
              size="small"
              title="已下发能力包（binding 层）"
              style={{ flex: "1 1 460px", minWidth: 420 }}
              extra={
                bindings ? <Tag color="green" style={{ marginInlineEnd: 0 }}>{boundRows.length} 项在绑</Tag> : null
              }
            >
              {capLoading ? (
                <Skeleton active paragraph={{ rows: 4 }} />
              ) : !bindings || boundRows.length === 0 ? (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      暂无在绑能力包——由管理 Agent 经审批通过后在此下发（spec-05 §2.1-§2.3，能力市场“申请绑定”）
                    </Typography.Text>
                  }
                  style={{ padding: "24px 0" }}
                />
              ) : (
                <Table<CapabilityBindingItem>
                  rowKey="binding_id"
                  size="small"
                  columns={capColumns}
                  dataSource={boundRows}
                  pagination={false}
                  scroll={{ y: 300, x: 640 }}
                />
              )}
            </Card>
          </Flex>

          {!isManager && (
            <Card
              size="small"
              title="EVOQUANT 验证窗（spec-05 §4.2/§4.3）"
              style={{ marginTop: 12 }}
              extra={
                windows ? (
                  <Tag
                    color={winRunning ? "blue" : "default"}
                    style={{ marginInlineEnd: 0 }}
                  >
                    {winRunning ? `在跑 ${winRunning} 窗 · 共 ${winRows.length}` : `窗 ×${winRows.length}`}
                  </Tag>
                ) : undefined
              }
            >
              {winLoading ? (
                <Skeleton active paragraph={{ rows: 3 }} />
              ) : winRows.length === 0 ? (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="尚无验证窗——EVOQUANT 对候选版本发起验证后在此呈现推进与判定（sealed 封存/rollback 否决/activate 晋升）"
                  style={{ padding: "20px 0" }}
                />
              ) : (
                <Table<ValidationWindow>
                  rowKey="id"
                  size="small"
                  columns={winColumns}
                  dataSource={winRows}
                  pagination={false}
                  scroll={{ x: 1080, y: 260 }}
                />
              )}
            </Card>
          )}
        </>
      )}
    </Drawer>
  );
}

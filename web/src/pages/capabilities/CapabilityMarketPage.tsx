import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Drawer,
  Empty,
  Flex,
  Input,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Skeleton,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import {
  fetchCapabilities,
  fetchCapabilityDetail,
  listAgents,
  submitApproval,
  unbindCapability,
  type AgentInfo,
  type CapabilityBindingBrief,
  type CapabilityDetail,
  type CapabilityItem,
} from "../../api/endpoints";

const APPLY_SHORT: Record<string, string> = {
  hash_hit_approved: "该能力此前已被批准，无需重复申请（确定性短路）。",
  hash_hit_rejected: "相同申请此前已被驳回（确定性短路）；如需调整请提交不同理由/目标。",
  cooldown: "同类申请被驳回后 24h 冷却中，如需提前放行请在审批中心人工豁免。",
  pending_full: "该 Agent 同类待决审批已达上限（≤3），请先处理待办。",
};
import { fmtBeijingTime } from "../../utils/time";

const TYPE_META: Record<string, { color: string; label: string }> = {
  skill: { color: "cyan", label: "Skill 指令" },
  tool: { color: "blue", label: "Tool 函数" },
  mcp: { color: "purple", label: "MCP 服务器" },
  datasource: { color: "geekblue", label: "数据源订阅" },
};
const SOURCE_LABEL: Record<string, string> = {
  opensource: "开源引入", api: "第三方 API", selfmade: "自研",
};
const SANDBOX_META: Record<string, { color: string; text: string }> = {
  pending: { color: "gold", text: "待沙箱" },
  passed: { color: "green", text: "沙箱通过" },
  failed: { color: "red", text: "沙箱失败" },
};

/** 上架四门槛（spec-05 §2.2）：齐备才可下发。 */
function gates(c: CapabilityItem): boolean[] {
  return [
    Boolean(c.name && c.description && c.version && c.maintainer),
    c.sandbox_status === "passed",
    Boolean(c.source_ref),
    c.active_bindings > 0,
  ];
}
const GATE_LABEL = ["① 元数据", "② 沙箱", "③ 来源留痕", "④ 绑定"];

export default function CapabilityMarketPage() {
  const { message } = AntApp.useApp();
  const [items, setItems] = useState<CapabilityItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [typeFilter, setTypeFilter] = useState<"all" | string>("all");
  const [statusFilter, setStatusFilter] = useState<"all" | string>("all");
  const [kw, setKw] = useState("");

  const [detail, setDetail] = useState<CapabilityDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [applyOpen, setApplyOpen] = useState(false);
  const [applyAgent, setApplyAgent] = useState("");
  const [applyReason, setApplyReason] = useState("");
  const [applySending, setApplySending] = useState(false);

  useEffect(() => {
    listAgents()
      .then((r) => setAgents(r.agents.filter((a) => a.role === "strategy")))
      .catch(() => undefined);
  }, []);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetchCapabilities({
        type: typeFilter === "all" ? undefined : typeFilter,
        status: statusFilter === "all" ? undefined : statusFilter,
        keyword: kw || undefined,
      });
      setItems(r.items);
    } catch (e) {
      message.error((e as Error).message ?? "加载能力市场失败");
    } finally {
      setLoading(false);
    }
  }, [typeFilter, statusFilter, kw, message]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const openDetail = async (c: CapabilityItem) => {
    setDetailLoading(true);
    setDetail({ capability: c, bindings: [] });
    try {
      setDetail(await fetchCapabilityDetail(c.id));
    } catch (err) {
      message.error((err as Error).message ?? "加载能力详情失败");
    } finally {
      setDetailLoading(false);
    }
  };

  const openApply = () => {
    setApplyAgent("");
    setApplyReason("");
    setApplyOpen(true);
  };

  const doApply = async (c: CapabilityItem) => {
    if (!applyAgent || !applyReason.trim()) {
      message.warning("请选择目标 Agent 并填写申请理由（策略依据）");
      return;
    }
    setApplySending(true);
    try {
      const r = await submitApproval({
        type: "capability",
        agent_id: applyAgent,
        payload: { capability_id: c.id },
        reason: applyReason.trim(),
      });
      if (r.ok) {
        message.success("能力申请单已提交待决——请在审批中心通过后自动下发（spec-05 §2.3）");
        setApplyOpen(false);
      } else if (r.reason) {
        message.warning(APPLY_SHORT[r.reason] ?? r.detail ?? "申请被确定性短路退回");
      } else {
        message.error(r.detail ?? "提交失败");
      }
    } catch (err) {
      message.error((err as Error).message ?? "提交能力申请失败");
    } finally {
      setApplySending(false);
    }
  };

  const refreshDetail = useCallback(async (id: string) => {
    setDetailLoading(true);
    try {
      setDetail(await fetchCapabilityDetail(id));
    } catch (err) {
      message.error((err as Error).message ?? "刷新能力详情失败");
    } finally {
      setDetailLoading(false);
    }
  }, [message]);

  const doUnbind = async (capabilityId: string, b: CapabilityBindingBrief) => {
    try {
      await unbindCapability(capabilityId, b.agent_id);
      message.success(`已解绑 ${b.agent_name}（${b.agent_id}）——留痕可查，spec-05 §2.4`);
      await refreshDetail(capabilityId);
      await reload();
    } catch (err) {
      message.error((err as Error).message ?? "解绑失败");
    }
  };

  const stats = useMemo(() => {
    const active = items.filter((c) => c.status === "active");
    const passed = items.filter((c) => c.sandbox_status === "passed");
    const bound = items.reduce((a, c) => a + c.active_bindings, 0);
    return { total: items.length, active: active.length, passed: passed.length, bound };
  }, [items]);

  const columns: ColumnsType<CapabilityItem> = [
    {
      title: "能力", key: "name", width: 320,
      render: (_, c) => (
        <Flex vertical gap={2}>
          <Flex align="center" gap={6}>
            <Typography.Link strong onClick={() => void openDetail(c)}>
              {c.name}
            </Typography.Link>
            <Tag color="purple" style={{ marginInlineEnd: 0 }}>{c.version}</Tag>
            {c.status === "deprecated" && <Tag color="red" style={{ marginInlineEnd: 0 }}>已废弃</Tag>}
          </Flex>
          <Typography.Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: c.description }}>
            {c.description || "—"}
          </Typography.Text>
        </Flex>
      ),
    },
    {
      title: "类型", dataIndex: "type", width: 120,
      render: (t: string) => {
        const m = TYPE_META[t] ?? { color: "default", label: t };
        return <Tag color={m.color}>{m.label}</Tag>;
      },
    },
    {
      title: "来源", key: "source", width: 170,
      render: (_, c) => (
        <Flex vertical gap={2}>
          <span style={{ fontSize: 12 }}>{SOURCE_LABEL[c.source_type] ?? c.source_type}</span>
          <Tooltip title={c.source_ref}>
            <Typography.Text type="secondary" style={{ fontSize: 11 }} ellipsis>
              {c.source_ref || "（未留痕）"}
            </Typography.Text>
          </Tooltip>
        </Flex>
      ),
    },
    {
      title: "沙箱", dataIndex: "sandbox_status", width: 100,
      render: (s: string) => {
        const m = SANDBOX_META[s] ?? { color: "default", text: s };
        return <Tag color={m.color}>{m.text}</Tag>;
      },
    },
    {
      title: "四门槛", key: "gates", width: 190,
      render: (_, c) => {
        const g = gates(c);
        return (
          <Space size={3} wrap>
            {g.map((ok, i) => (
              <Tooltip key={i} title={GATE_LABEL[i]}>
                <span
                  style={{
                    fontSize: 11,
                    color: ok ? "#52c41a" : "rgba(0,0,0,0.25)",
                    border: `1px solid ${ok ? "#b7eb8f" : "rgba(0,0,0,0.15)"}`,
                    borderRadius: 3,
                    padding: "0 4px",
                  }}
                >
                  {GATE_LABEL[i]}
                </span>
              </Tooltip>
            ))}
          </Space>
        );
      },
    },
    {
      title: "在绑", dataIndex: "active_bindings", width: 70,
      render: (v: number) => (v > 0 ? <Tag color="green">{v}</Tag> : <Typography.Text type="secondary">0</Typography.Text>),
    },
    { title: "维护者", dataIndex: "maintainer", width: 130, render: (v: string) => v || "—" },
    { title: "更新时间", dataIndex: "updated_ts", width: 160, render: (v: string) => fmtBeijingTime(v) },
  ];

  const cap = detail?.capability ?? null;

  return (
    <div style={{ padding: 16, minHeight: "100%" }}>
      <Flex justify="space-between" align="flex-start" gap={12} wrap style={{ marginBottom: 12 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            能力市场
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            能力注册目录（spec-05 §2）：Skill / Tool / MCP / 数据源四类；四门槛齐备才可下发（spec-06 §6.8）
          </Typography.Text>
        </div>
      </Flex>

      <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: 8 } }}>
        <Flex gap={24} wrap align="center">
          <Statistic title="能力" value={stats.total} style={{ minWidth: 70 }} />
          <Statistic title="可下发" value={stats.active} valueStyle={{ color: "#52c41a" }} style={{ minWidth: 70 }} />
          <Statistic title="沙箱通过" value={stats.passed} style={{ minWidth: 90 }} />
          <Statistic title="绑定数" value={stats.bound} style={{ minWidth: 80 }} />
        </Flex>
      </Card>

      <Card size="small" title="能力目录">
        <Flex gap={8} wrap style={{ marginBottom: 12 }}>
          <Radio.Group value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} optionType="button" size="small">
            <Radio.Button value="all">全部类型</Radio.Button>
            {Object.entries(TYPE_META).map(([k, m]) => (
              <Radio.Button key={k} value={k}>{m.label}</Radio.Button>
            ))}
          </Radio.Group>
          <Radio.Group value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} optionType="button" size="small">
            <Radio.Button value="all">全部状态</Radio.Button>
            <Radio.Button value="active">可下发</Radio.Button>
            <Radio.Button value="deprecated">已废弃</Radio.Button>
          </Radio.Group>
          <Input
            allowClear
            size="small"
            placeholder="搜索名称/描述"
            prefix={<SearchOutlined />}
            style={{ width: 220 }}
            value={kw}
            onChange={(e) => setKw(e.target.value)}
          />
          <Button size="small" icon={<ReloadOutlined />} onClick={() => void reload()}>
            刷新
          </Button>
        </Flex>
        <Table<CapabilityItem>
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={items}
          pagination={{ pageSize: 12, hideOnSinglePage: true }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="能力市场为空——管理 Agent 注册（四门槛）后在此陈列" /> }}
          scroll={{ x: 1160 }}
        />
      </Card>

      <Drawer
        title={cap ? `${cap.id} · ${cap.name}` : ""}
        width={640}
        open={detail !== null}
        onClose={() => setDetail(null)}
      >
        {detailLoading && <Skeleton active paragraph={{ rows: 8 }} />}
        {!detailLoading && cap && (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Flex gap={8} wrap>
              <Tag color={TYPE_META[cap.type]?.color ?? "default"}>{TYPE_META[cap.type]?.label ?? cap.type}</Tag>
              <Tag color="purple">{cap.version}</Tag>
              <Tag color={SANDBOX_META[cap.sandbox_status]?.color ?? "default"}>
                {SANDBOX_META[cap.sandbox_status]?.text ?? cap.sandbox_status}
              </Tag>
              <Tag color={cap.status === "deprecated" ? "red" : "green"}>
                {cap.status === "deprecated" ? "已废弃" : "可下发"}
              </Tag>
              <Tag>维护者：{cap.maintainer || "—"}</Tag>
            </Flex>

            {cap.status === "deprecated" && (
              <Alert type="warning" showIcon message="已废弃（spec-05 §2.4）" description="不再对新 Agent 下发；存量绑定保留并通知迁移。" />
            )}

            <Alert
              type="info"
              showIcon
              message="上架四门槛（spec-05 §2.2）"
              description={
                <span>
                  {gates(cap).map((ok, i) => (
                    <span key={i} style={{ marginRight: 10, color: ok ? "#389e0d" : "rgba(0,0,0,0.45)" }}>
                      {ok ? "齐" : "缺"} {GATE_LABEL[i]}
                    </span>
                  ))}
                </span>
              }
            />

            <Card size="small" title="能力描述">
              <Typography.Paragraph style={{ marginBottom: 0 }}>{cap.description || "（未填写）"}</Typography.Paragraph>
            </Card>

            <Card size="small" title="来源与 License（门槛③ 留痕）">
              <Typography.Paragraph style={{ marginBottom: 0 }}>
                {SOURCE_LABEL[cap.source_type] ?? cap.source_type}：{cap.source_ref || "（未留痕）"}
              </Typography.Paragraph>
            </Card>

            <Card size="small" title="元数据（输入输出/依赖/权限）">
              {cap.metadata && Object.keys(cap.metadata).length ? (
                <pre style={{ margin: 0, fontSize: 12, whiteSpace: "pre-wrap" }}>
                  {JSON.stringify(cap.metadata, null, 2)}
                </pre>
              ) : (
                <Typography.Text type="secondary">无额外元数据。</Typography.Text>
              )}
            </Card>

            <Card size="small" title={`绑定清单（${cap.active_bindings} 在绑）`}>
              {detail!.bindings.length === 0 ? (
                <Typography.Text type="secondary">暂无绑定记录——下发走管理 Agent + spec-04 审批流（spec-05 §2.3）。</Typography.Text>
              ) : (
                <Space direction="vertical" size={4} style={{ width: "100%" }}>
                  {detail!.bindings.map((b) => (
                    <Flex key={b.binding_id} wrap gap={8} align="center">
                      <Tag color={b.active ? "green" : "default"} style={{ marginInlineEnd: 0 }}>
                        {b.active ? "在绑" : "已解绑"}
                      </Tag>
                      <Typography.Text style={{ fontSize: 13 }}>{b.agent_name}（{b.agent_id}）</Typography.Text>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        由 {b.bound_by} · {fmtBeijingTime(b.bound_ts)}
                      </Typography.Text>
                      {!b.active && b.unbound_ts && (
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          · 解绑于 {fmtBeijingTime(b.unbound_ts)}
                        </Typography.Text>
                      )}
                      {b.active && (
                        <Popconfirm
                          title="确认解绑该能力？"
                          description="置 unbound_ts 留痕（可查可回滚），Agent 将不再持有该能力。"
                          okText="解绑"
                          okButtonProps={{ danger: true }}
                          onConfirm={() => void doUnbind(cap.id, b)}
                        >
                          <Button size="small" danger style={{ marginLeft: "auto" }}>
                            解绑
                          </Button>
                        </Popconfirm>
                      )}
                    </Flex>
                  ))}
                </Space>
              )}
            </Card>

            {cap.sandbox_report_ref && (
              <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
                沙箱报告：{cap.sandbox_report_ref}
              </Typography.Paragraph>
            )}

            <Flex justify="space-between" align="center" gap={8}>
              <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
                注册/绑定/解绑/回滚由管理 Agent + spec-04 审批流执行（spec-05 §2.3）；
                子 Agent 申请下发经审批通过后在此自动绑定。
              </Typography.Paragraph>
              <Tooltip
                title={
                  cap.status === "deprecated"
                    ? "已废弃能力不再下发（存量绑定保留）"
                    : cap.sandbox_status !== "passed"
                      ? "沙箱未通过，暂不可申请下发"
                      : undefined
                }
              >
                <Button
                  type="primary"
                  disabled={cap.status === "deprecated" || cap.sandbox_status !== "passed"}
                  onClick={() => openApply()}
                >
                  申请绑定
                </Button>
              </Tooltip>
            </Flex>
          </Space>
        )}
      </Drawer>

      <Modal
        title={cap ? `申请绑定 · ${cap.name}` : ""}
        open={applyOpen && cap !== null}
        onCancel={() => setApplyOpen(false)}
        okText="提交申请"
        confirmLoading={applySending}
        onOk={() => cap && void doApply(cap)}
      >
        {cap && (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Alert
              type="info"
              showIcon
              message="下发闭环（spec-05 §2.3）"
              description="提交后生成 capability 审批单（24h 未决自动过期）；审批中心人工评审通过即绑定，驳回留痕并进入 24h 冷却。相同申请此前已决将被确定性短路拦截。"
            />
            <div>
              <Typography.Text strong>目标 Agent</Typography.Text>
              <Select
                style={{ width: "100%", marginTop: 4 }}
                placeholder="选择需要该能力的策略 Agent"
                value={applyAgent || undefined}
                onChange={setApplyAgent}
                options={agents.map((a) => ({
                  value: a.id,
                  label: `${a.name}（${a.id}）`,
                }))}
              />
            </div>
            <div>
              <Typography.Text strong>申请理由（策略依据）</Typography.Text>
              <Input.TextArea
                style={{ marginTop: 4 }}
                rows={3}
                maxLength={500}
                showCount
                placeholder="说明该能力将如何被使用、解决什么问题"
                value={applyReason}
                onChange={(e) => setApplyReason(e.target.value)}
              />
            </div>
          </Space>
        )}
      </Modal>
    </div>
  );
}

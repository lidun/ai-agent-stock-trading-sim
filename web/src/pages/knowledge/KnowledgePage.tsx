import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Drawer,
  Empty,
  Flex,
  Form,
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
import {
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import {
  createKb,
  deleteKb,
  fetchKb,
  listKb,
  restoreKb,
  transitionKb,
  upsertKbStats,
  type KbEntry,
  type KbStatus,
  type KbStatsRow,
  type KbType,
} from "../../api/endpoints";
import { fmtBeijingTime } from "../../utils/time";

const STATUS_META: Record<KbStatus, { color: string; text: string }> = {
  observing: { color: "default", text: "观察中" },
  validating: { color: "blue", text: "验证中" },
  valid: { color: "green", text: "有效" },
  invalid: { color: "red", text: "已失效" },
  sealed: { color: "gold", text: "已封存" },
};

const TYPE_LABEL: Record<string, string> = { positive: "正向概念", pitfall: "反向避坑" };
const SOURCE_LABEL: Record<string, string> = {
  user: "用户录入",
  retrospective: "归档/复盘",
  manager_observation: "管理观察",
  market_anomaly: "市场异动",
};
const SEVERITY_LABEL: Record<string, string> = {
  high: "高危", mid: "中危", low: "低危",
};
const SEVERITY_COLOR: Record<string, string> = { high: "red", mid: "orange", low: "green" };

function money(v: number | null | undefined, digits = 2): string {
  return v == null ? "—" : v.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function pct(v: number | null | undefined): string {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}

/** 条目可用动作（按状态机 §3.2） */
function availableActions(status: KbStatus): { action: string; label: string; danger?: boolean; primary?: boolean }[] {
  switch (status) {
    case "observing":
      return [{ action: "start_validation", label: "启动验证（闸2 评审）", primary: true }];
    case "validating":
      return [
        { action: "approve_valid", label: "确认有效", primary: true },
        { action: "seal", label: "封存（证据不足）" },
        { action: "invalidate", label: "判定失效", danger: true },
      ];
    case "valid":
      return [{ action: "invalidate", label: "失效（滚动监控）", danger: true }];
    case "invalid":
    case "sealed":
      return [{ action: "start_validation", label: "复核再验证", primary: true }];
    default:
      return [];
  }
}

const ACTION_HINT: Record<string, string> = {
  start_validation: "闸2 管理评审：逻辑一致性 / 可证伪性 / 与现有条目查重结论。invalid/sealed 复核进入需说明新证据周期。",
  approve_valid: "statistics-driven 结论确认：单桶 n≥30 且期望值为正（可附统计摘要），证据来自 kb_stats/signal_registry。",
  invalidate: "判定失效须填写原因（如滚动 60 日期望值 <0、逻辑被证伪）。",
  seal: "证据不足封存须填写原因（如验证预算到期、样本不足 30）。",
};

interface StatFormVals {
  env_bucket?: string;
  sample_n?: number;
  win_rate?: number;
  avg_win?: number;
  avg_loss?: number;
  expectancy?: number;
  intercept_n?: number;
  exception_n?: number;
  stale_n?: number;
  dispatch_n?: number;
  window_days?: number;
  note?: string;
}

interface CreateVals {
  type: KbType;
  name: string;
  description?: string;
  source: string;
  severity?: "high" | "mid" | "low";
  env_scope?: string;
  origin_agent?: string;
  trigger_rule?: string;
  computation?: string;
  data_sources?: string;
}

export default function KnowledgePage() {
  const { message } = AntApp.useApp();
  const [entries, setEntries] = useState<KbEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [typeFilter, setTypeFilter] = useState<"all" | KbType>("all");
  const [statusFilter, setStatusFilter] = useState<"all" | KbStatus>("all");
  const [kw, setKw] = useState("");
  const [showDeleted, setShowDeleted] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [form] = Form.useForm<CreateVals>();

  const [detail, setDetail] = useState<KbEntry | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [transOpen, setTransOpen] = useState<KbEntry | null>(null);
  const [transAction, setTransAction] = useState("");
  const [transNote, setTransNote] = useState("");
  const [transLoading, setTransLoading] = useState(false);
  const [statOpen, setStatOpen] = useState(false);
  const [statForm] = Form.useForm<StatFormVals>();
  const [statSaving, setStatSaving] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const r = await listKb({
        type: typeFilter === "all" ? undefined : typeFilter,
        status: statusFilter === "all" ? undefined : statusFilter,
        kw: kw || undefined,
        includeDeleted: showDeleted,
      });
      setEntries(r.entries);
    } catch (e) {
      message.error((e as Error).message ?? "加载知识库失败");
    } finally {
      setLoading(false);
    }
  }, [typeFilter, statusFilter, kw, showDeleted, message]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const openDetail = async (e: KbEntry) => {
    setDetailLoading(true);
    setDetail(e);
    try {
      const r = await fetchKb(e.id);
      setDetail(r.entry);
    } catch (err) {
      message.error((err as Error).message ?? "加载详情失败");
    } finally {
      setDetailLoading(false);
    }
  };

  const doCreate = async () => {
    const v = await form.validateFields();
    setCreating(true);
    try {
      const spec =
        v.trigger_rule || v.computation || v.data_sources
          ? {
              trigger_rule: v.trigger_rule || "",
              computation: v.computation || "",
              data_sources: (v.data_sources || "")
                .split(/[,，]/)
                .map((s) => s.trim())
                .filter(Boolean),
            }
          : undefined;
      const r = await createKb({
        name: v.name,
        type: v.type,
        description: v.description || "",
        source: v.source || "user",
        severity: v.severity || "",
        env_scope: v.env_scope || "all",
        origin_agent: v.origin_agent || undefined,
        computable_spec: spec,
      });
      message.success(`已入库 ${r.entry.id}（观察中，闸1 通过）`);
      setCreateOpen(false);
      form.resetFields();
      void reload();
    } catch (e) {
      message.error((e as Error).message ?? "入库失败");
    } finally {
      setCreating(false);
    }
  };

  const openTransition = (e: KbEntry, action: string) => {
    setTransOpen(e);
    setTransAction(action);
    setTransNote("");
  };

  const doTransition = async () => {
    if (!transOpen) return;
    if (!transNote.trim()) {
      message.warning("请填写评审/原因说明（审计留痕）");
      return;
    }
    setTransLoading(true);
    try {
      await transitionKb(
        transOpen.id,
        transAction as "start_validation" | "approve_valid" | "invalidate" | "seal",
        transNote,
      );
      message.success("状态已迁移");
      setTransOpen(null);
      void reload();
      if (detail?.id === transOpen.id) void openDetail(transOpen);
    } catch (e) {
      message.error((e as Error).message ?? "迁移失败");
    } finally {
      setTransLoading(false);
    }
  };

  const doDelete = async (e: KbEntry) => {
    try {
      await deleteKb(e.id, "管理软删");
      message.success(`${e.id} 已软删（退出下发与排序，历史统计保留）`);
      void reload();
      if (detail?.id === e.id) setDetail((d) => (d ? { ...d, deleted_ts: e.updated_ts } : d));
    } catch (err) {
      message.error((err as Error).message ?? "删除失败");
    }
  };

  const doRestore = async (e: KbEntry) => {
    try {
      await restoreKb(e.id, "复核重新激活");
      message.success(`${e.id} 已恢复`);
      void reload();
      if (detail?.id === e.id) void openDetail(e);
    } catch (err) {
      message.error((err as Error).message ?? "恢复失败");
    }
  };

  const doSaveStats = async () => {
    if (!detail) return;
    const v = await statForm.validateFields();
    setStatSaving(true);
    try {
      await upsertKbStats(detail.id, { ...v, env_bucket: v.env_bucket || "all" } as never);
      message.success("统计快照已写入");
      setStatOpen(false);
      statForm.resetFields();
      void openDetail(detail);
    } catch (e) {
      message.error((e as Error).message ?? "写入失败");
    } finally {
      setStatSaving(false);
    }
  };

  const statsSummary = useMemo(() => {
    const m = new Map<string, { buckets: number; n: number; intercept: number; exception: number; expectancy: number | null }>();
    entries.forEach((e) => {
      const sts = e.stats ?? [];
      m.set(e.id, {
        buckets: sts.length,
        n: sts.reduce((a, s) => a + s.sample_n, 0),
        intercept: sts.reduce((a, s) => a + s.intercept_n, 0),
        exception: sts.reduce((a, s) => a + s.exception_n, 0),
        expectancy: sts.reduce<number | null>((acc, s) => {
          if (s.expectancy == null) return acc;
          return acc == null ? s.expectancy : acc + s.expectancy;
        }, null),
      });
    });
    return m;
  }, [entries]);

  const columns: ColumnsType<KbEntry> = [
    {
      title: "条目", key: "entry", width: 320,
      render: (_, e) => (
        <Flex vertical gap={2}>
          <Flex align="center" gap={6}>
            <Typography.Link strong onClick={() => void openDetail(e)}>
              {e.name}
            </Typography.Link>
            {e.type === "pitfall" && e.severity && (
              <Tag color={SEVERITY_COLOR[e.severity]} style={{ marginInlineEnd: 0 }}>
                {SEVERITY_LABEL[e.severity]}
              </Tag>
            )}
          </Flex>
          <Typography.Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: e.description }}>
            {e.description || "—"}
          </Typography.Text>
        </Flex>
      ),
    },
    { title: "ID", dataIndex: "id", width: 90, render: (s: string) => <Typography.Text code>{s}</Typography.Text> },
    {
      title: "类型", dataIndex: "type", width: 100,
      render: (t: string) => (t === "pitfall" ? <Tag color="volcano">{TYPE_LABEL[t]}</Tag> : <Tag color="cyan">{TYPE_LABEL[t]}</Tag>),
    },
    {
      title: "状态", dataIndex: "status", width: 110,
      render: (s: KbStatus, e) => {
        const m = STATUS_META[s];
        return e.deleted_ts ? <Tag color="purple">已软删</Tag> : <Tag color={m.color}>{m.text}</Tag>;
      },
    },
    {
      title: "统计", key: "stats", width: 200,
      render: (_, e) => {
        const s = statsSummary.get(e.id);
        if (!s || s.n === 0) return <Typography.Text type="secondary">无样本</Typography.Text>;
        const expectVal = s.expectancy;
        return (
          <Space size={6} wrap>
            <span style={{ fontSize: 12 }}>n={s.n}</span>
            <Tooltip title={`各桶期望值合计（含费）`}>
              <Typography.Text style={{ fontSize: 12, color: expectVal != null && expectVal >= 0 ? "#cf1322" : "#389e0d" }}>
                E={expectVal == null ? "—" : expectVal.toFixed(2)}
              </Typography.Text>
            </Tooltip>
            {e.type === "pitfall" && s.intercept + s.exception > 0 && (
              <Tag color="gold" style={{ fontSize: 11 }}>拦截{s.intercept}/破例{s.exception}</Tag>
            )}
          </Space>
        );
      },
    },
    {
      title: "来源", dataIndex: "source", width: 110,
      render: (s: string) => SOURCE_LABEL[s] ?? s,
    },
    { title: "更新时间", dataIndex: "updated_ts", width: 160, render: (v: string) => fmtBeijingTime(v) },
    {
      title: "操作", key: "op", width: 240,
      render: (_, e) => (
        <Space size={2} wrap>
          {!e.deleted_ts ? (
            <>
              {availableActions(e.status).map((a) => (
                <Button
                  key={a.action}
                  size="small"
                  type={a.primary ? "primary" : "default"}
                  danger={a.danger}
                  onClick={() => openTransition(e, a.action)}
                >
                  {a.label}
                </Button>
              ))}
              <Popconfirm title="软删该条目？" description="历史统计与信号引用保留；可恢复。" okText="软删" okButtonProps={{ danger: true }} onConfirm={() => void doDelete(e)}>
                <Button size="small" danger ghost>
                  删除
                </Button>
              </Popconfirm>
            </>
          ) : (
            <Button size="small" type="primary" ghost onClick={() => void doRestore(e)}>
              恢复
            </Button>
          )}
        </Space>
      ),
    },
  ];

  const watchedType = Form.useWatch("type", form);

  if (loading && entries.length === 0) {
    return (
      <div style={{ padding: 16 }}>
        <Skeleton active paragraph={{ rows: 10 }} />
      </div>
    );
  }

  const activeCount = entries.filter((e) => !e.deleted_ts).length;
  const validCount = entries.filter((e) => e.status === "valid" && !e.deleted_ts).length;
  const pitfallCount = entries.filter((e) => e.type === "pitfall" && !e.deleted_ts).length;
  const statRow = detail?.stats?.length
    ? detail.stats.reduce((a, s) => ({ ...a, sample_n: a.sample_n + s.sample_n, intercept_n: a.intercept_n + s.intercept_n, exception_n: a.exception_n + s.exception_n, stale_n: a.stale_n + s.stale_n, dispatch_n: a.dispatch_n + s.dispatch_n }), { sample_n: 0, intercept_n: 0, exception_n: 0, stale_n: 0, dispatch_n: 0 })
    : null;

  return (
    <div style={{ padding: 16, minHeight: "100%" }}>
      <Flex justify="space-between" align="flex-start" gap={12} wrap style={{ marginBottom: 12 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            知识库
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            正向概念库 + 反向避坑库（spec-05 §3）；状态徽标：有效绿 / 失效红 / 观察灰 / 封存黄 / 验证蓝
          </Typography.Text>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
          新建知识条目
        </Button>
      </Flex>

      <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: 8 } }}>
        <Flex gap={12} wrap align="center">
          <Statistic title="库内条目" value={activeCount} style={{ minWidth: 90 }} />
          <Statistic title="有效" value={validCount} valueStyle={{ color: "#52c41a" }} style={{ minWidth: 70 }} />
          <Statistic title="反向避坑" value={pitfallCount} style={{ minWidth: 90 }} />
        </Flex>
      </Card>

      <Card size="small" title="条目列表">
        <Flex gap={8} wrap style={{ marginBottom: 12 }}>
          <Radio.Group value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} optionType="button" size="small">
            <Radio.Button value="all">全部类型</Radio.Button>
            <Radio.Button value="positive">正向概念</Radio.Button>
            <Radio.Button value="pitfall">反向避坑</Radio.Button>
          </Radio.Group>
          <Radio.Group value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} optionType="button" size="small">
            {(["all", "observing", "validating", "valid", "invalid", "sealed"] as const).map((s) => (
              <Radio.Button key={s} value={s}>
                {s === "all" ? "全部状态" : STATUS_META[s].text}
              </Radio.Button>
            ))}
          </Radio.Group>
          <Input
            allowClear
            size="small"
            placeholder="搜索名称/描述/ID"
            prefix={<SearchOutlined />}
            style={{ width: 200 }}
            value={kw}
            onChange={(e) => setKw(e.target.value)}
          />
          <Space>
            <Button size="small" type={showDeleted ? "primary" : "default"} onClick={() => setShowDeleted((v) => !v)}>
              含已软删
            </Button>
            <Button size="small" icon={<ReloadOutlined />} onClick={() => void reload()}>
              刷新
            </Button>
          </Space>
        </Flex>
        <Table<KbEntry>
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={entries}
          pagination={{ pageSize: 12, hideOnSinglePage: true }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="知识库暂无条目——点击右上角新建" /> }}
          scroll={{ x: 1180 }}
        />
      </Card>

      <Drawer
        title={detail ? `${detail.id} · ${detail.name}` : ""}
        width={640}
        open={detail !== null}
        onClose={() => setDetail(null)}
        extra={
          <Space>
            {detail && !detail.deleted_ts && (
              <Button size="small" icon={<SafetyCertificateOutlined />} onClick={() => setStatOpen(true)}>
                管理统计
              </Button>
            )}
            {detail && !detail.deleted_ts && (
              <Button size="small" danger onClick={() => void doDelete(detail)}>
                软删
              </Button>
            )}
          </Space>
        }
      >
        {detailLoading && <Skeleton active paragraph={{ rows: 8 }} />}
        {!detailLoading && detail && (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Flex gap={8} wrap>
              <Tag color={detail.type === "pitfall" ? "volcano" : "cyan"}>{detail.type_label}</Tag>
              <Tag color={STATUS_META[detail.status].color}>{detail.deleted_ts ? "已软删" : detail.status_label}</Tag>
              <Tag>{SOURCE_LABEL[detail.source] ?? detail.source}</Tag>
              {detail.severity && <Tag color={SEVERITY_COLOR[detail.severity]}>severity: {SEVERITY_LABEL[detail.severity]}</Tag>}
              <Tag>环境域：{detail.env_scope}</Tag>
              {detail.origin_agent && <Tag>{detail.origin_agent}</Tag>}
            </Flex>

            <Alert
              type="info"
              showIcon
              message="参考非指令（spec-05 §3.5）"
              description="入库条目以下发“候选参考卡”使用——KB-id/状态/统计摘要，供子 Agent 决策引用并留痕；状态晋升由 signal_registry 客观统计驱动 + 本机人工评审确认。"
            />

            <Card size="small" title="概念描述">
              <Typography.Paragraph style={{ marginBottom: 0 }}>{detail.description || "（未填写）"}</Typography.Paragraph>
            </Card>

            <Card size="small" title="事前可计算性规格（闸1）">
              {detail.computable_spec && Object.keys(detail.computable_spec).length ? (
                <Space direction="vertical" size={6} style={{ width: "100%" }}>
                  {["trigger_rule", "computation", "data_sources"].map((k) => {
                    const label = { trigger_rule: "触发识别条件", computation: "计算口径", data_sources: "所需数据" }[k] ?? k;
                    const v = detail.computable_spec[k];
                    return (
                      <div key={k}>
                        <Typography.Text strong style={{ fontSize: 12 }}>{label}</Typography.Text>
                        <Typography.Paragraph style={{ marginBottom: 0, fontSize: 13 }}>
                          {Array.isArray(v) ? v.join("、") : String(v ?? "—")}
                        </Typography.Paragraph>
                      </div>
                    );
                  })}
                </Space>
              ) : (
                <Typography.Text type="secondary">纯方法论条目，无 computable_spec。</Typography.Text>
              )}
              <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
                闸1 校验：{JSON.stringify(detail.review_gate1_ref.checks ?? [])}
              </Typography.Paragraph>
            </Card>

            {detail.invalid_reason && (
              <Alert type="error" showIcon message="失效原因" description={detail.invalid_reason} />
            )}
            {detail.sealed_reason && (
              <Alert type="warning" showIcon message="封存原因" description={detail.sealed_reason} />
            )}
            {Object.keys(detail.review_gate2_ref).length > 0 && (
              <Card size="small" title="闸2 评审（管理复核留痕）">
                <Typography.Paragraph style={{ marginBottom: 0 }}>
                  {String((detail.review_gate2_ref as { note?: string }).note ?? "—")} · 由{" "}
                  {(detail.review_gate2_ref as { by?: string }).by ?? "—"} 于{" "}
                  {(detail.review_gate2_ref as { ts?: string }).ts ?? "—"}
                </Typography.Paragraph>
              </Card>
            )}

            <Card size="small" title="验证统计快照（kb_stats，条目×环境桶）">
              {statRow && statRow.sample_n > 0 && (
                <Flex gap={18} wrap style={{ marginBottom: 12 }}>
                  <span>样本合计 n={statRow.sample_n}</span>
                  <span>拦截 {statRow.intercept_n}</span>
                  <span>破例 {statRow.exception_n}</span>
                  <span>stale {statRow.stale_n}</span>
                  <span>下发/引用 {statRow.dispatch_n}</span>
                </Flex>
              )}
              <Table<KbStatsRow>
                rowKey="id"
                size="small"
                pagination={false}
                dataSource={detail.stats ?? []}
                locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无统计快照——结算/信号统计写入后呈现" /> }}
                columns={[
                  { title: "环境桶", dataIndex: "env_bucket", width: 110 },
                  { title: "n", dataIndex: "sample_n", width: 60, align: "right" },
                  { title: "胜率", dataIndex: "win_rate", width: 80, align: "right", render: pct },
                  { title: "均盈", dataIndex: "avg_win", width: 90, align: "right", render: (v: number | null) => money(v) },
                  { title: "均亏", dataIndex: "avg_loss", width: 90, align: "right", render: (v: number | null) => money(v) },
                  {
                    title: "期望值", dataIndex: "expectancy", width: 90, align: "right",
                    render: (v: number | null) => (
                      <Typography.Text style={{ color: v != null && v >= 0 ? "#cf1322" : "#389e0d" }}>{money(v)}</Typography.Text>
                    ),
                  },
                  { title: "拦截", dataIndex: "intercept_n", width: 70, align: "right" },
                  { title: "破例", dataIndex: "exception_n", width: 70, align: "right" },
                  { title: "stale", dataIndex: "stale_n", width: 70, align: "right" },
                  { title: "下发 n_i", dataIndex: "dispatch_n", width: 90, align: "right" },
                  { title: "窗口", dataIndex: "window_days", width: 70, align: "right", render: (v: number | null) => v ?? "—" },
                ]}
              />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                口径（spec-05 §3.3）：样本 n 来自 signal_registry 前瞻收益；最小样本 n≥30 才可晋升 valid；
                stale-price 样本单列；被拦截（intercept）与破例（exception）分开统计；n_i=dispatch_n 供 UCB 排序。
              </Typography.Text>
            </Card>

            <Flex justify="space-between">
              <Space>
                {!detail.deleted_ts &&
                  availableActions(detail.status).map((a) => (
                    <Button
                      key={a.action}
                      type={a.primary ? "primary" : "default"}
                      danger={a.danger}
                      size="small"
                      onClick={() => openTransition(detail, a.action)}
                    >
                      {a.label}
                    </Button>
                  ))}
                {detail.deleted_ts && (
                  <Button size="small" type="primary" ghost onClick={() => void doRestore(detail)}>
                    恢复
                  </Button>
                )}
              </Space>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                创建于 {fmtBeijingTime(detail.created_ts)} · 更新于 {fmtBeijingTime(detail.updated_ts)}
              </Typography.Text>
            </Flex>
          </Space>
        )}
      </Drawer>

      <Modal
        title="新建知识条目"
        open={createOpen}
        onCancel={() => {
          setCreateOpen(false);
          form.resetFields();
        }}
        onOk={() => void doCreate()}
        confirmLoading={creating}
        okText="入库（观察中）"
        width={640}
        destroyOnClose
      >
        <Form form={form} layout="vertical" initialValues={{ type: "positive", source: "user", env_scope: "all" }}>
          <Form.Item name="type" label="类型" rules={[{ required: true }]}>
            <Radio.Group>
              <Radio.Button value="positive">正向概念</Radio.Button>
              <Radio.Button value="pitfall">反向避坑</Radio.Button>
            </Radio.Group>
          </Form.Item>
          <Form.Item name="name" label="名称（受控词表优先，同义变体将按规范名归并）" rules={[{ required: true, message: "请输入名称" }]}>
            <Input maxLength={80} />
          </Form.Item>
          <Form.Item name="description" label="概念/陷阱形态描述" rules={[{ required: true, message: "请输入描述" }]}>
            <Input.TextArea rows={2} maxLength={600} />
          </Form.Item>
          <Flex gap={12}>
            <Form.Item name="source" label="来源" style={{ flex: 1 }} rules={[{ required: true }]}>
              <Select options={Object.entries(SOURCE_LABEL).map(([value, label]) => ({ value, label }))} />
            </Form.Item>
            {watchedType === "pitfall" && (
              <Form.Item name="severity" label="severity（避坑必填）" style={{ flex: 1 }} rules={[{ required: true }]}>
                <Select options={Object.entries(SEVERITY_LABEL).map(([value, label]) => ({ value, label }))} />
              </Form.Item>
            )}
            <Form.Item name="env_scope" label="适用环境域" style={{ flex: 1 }}>
              <Input maxLength={40} placeholder="如 cn_a_main / all" />
            </Form.Item>
          </Flex>
          <Form.Item name="origin_agent" label="发现来源 Agent（可选）" style={{ marginBottom: 8 }}>
            <Input placeholder="agent 示例：agent-demo-001" maxLength={60} />
          </Form.Item>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
            事前可计算性规格（闸1）——避坑条目必填三要素；正向概念可选。
          </Typography.Paragraph>
          <Form.Item name="trigger_rule" label="触发识别条件（trigger_rule）" style={{ marginBottom: 8 }}>
            <Input.TextArea rows={1} placeholder="例：收盘前 5 分钟涨幅 < 0 且量能萎缩" />
          </Form.Item>
          <Form.Item name="computation" label="计算口径（computation）" style={{ marginBottom: 8 }}>
            <Input.TextArea rows={1} placeholder="例：按日内 5m 序列相邻分钟判定触达" />
          </Form.Item>
          <Form.Item name="data_sources" label="所需数据（data_sources，逗号分隔）" style={{ marginBottom: 0 }}>
            <Input placeholder="例：l1_minute, eod_daily" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={transOpen ? `状态迁移 · ${transOpen.id}` : ""}
        open={transOpen !== null}
        onCancel={() => setTransOpen(null)}
        onOk={() => void doTransition()}
        confirmLoading={transLoading}
        okText="确认"
        okButtonProps={{ danger: ["invalidate"].includes(transAction) }}
      >
        <Alert type="info" showIcon style={{ marginBottom: 12 }} message={ACTION_HINT[transAction]} />
        <Input.TextArea
          rows={3}
          placeholder="评审/原因说明（审计留痕，必填）"
          value={transNote}
          onChange={(e) => setTransNote(e.target.value)}
          maxLength={500}
        />
      </Modal>

      <Modal
        title={detail ? `管理统计快照 · ${detail.id}` : ""}
        open={statOpen}
        onCancel={() => {
          setStatOpen(false);
          statForm.resetFields();
        }}
        onOk={() => void doSaveStats()}
        confirmLoading={statSaving}
        okText="写入快照"
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          按 条目×环境桶 唯一；本页为人工维护入口，引擎确定性重算将自动覆盖。字段留空保留既有值。
        </Typography.Paragraph>
        <Form form={statForm} layout="vertical" initialValues={{ env_bucket: "all", sample_n: 0 }}>
          <Form.Item name="env_bucket" label="环境桶" rules={[{ required: true }]}>
            <Input placeholder="如 cn_a_main / all" />
          </Form.Item>
          <Flex gap={12} wrap>
            <Form.Item name="sample_n" label="样本 n" style={{ width: 130 }}>
              <Input type="number" min={0} />
            </Form.Item>
            <Form.Item name="win_rate" label="胜率 (0~1)" style={{ width: 150 }}>
              <Input type="number" min={0} max={1} step={0.01} />
            </Form.Item>
            <Form.Item name="expectancy" label="期望值（含费）" style={{ width: 150 }}>
              <Input type="number" step={0.01} />
            </Form.Item>
            <Form.Item name="window_days" label="窗口(交易日)" style={{ width: 150 }}>
              <Input type="number" min={1} />
            </Form.Item>
          </Flex>
          <Flex gap={12} wrap>
            <Form.Item name="avg_win" label="均盈" style={{ width: 130 }}>
              <Input type="number" step={0.01} />
            </Form.Item>
            <Form.Item name="avg_loss" label="均亏（可负）" style={{ width: 150 }}>
              <Input type="number" step={0.01} />
            </Form.Item>
            <Form.Item name="intercept_n" label="拦截样本" style={{ width: 140 }}>
              <Input type="number" min={0} />
            </Form.Item>
            <Form.Item name="exception_n" label="破例样本" style={{ width: 140 }}>
              <Input type="number" min={0} />
            </Form.Item>
          </Flex>
          <Flex gap={12} wrap>
            <Form.Item name="stale_n" label="stale 样本" style={{ width: 130 }}>
              <Input type="number" min={0} />
            </Form.Item>
            <Form.Item name="dispatch_n" label="下发/引用 n_i" style={{ width: 150 }}>
              <Input type="number" min={0} />
            </Form.Item>
            <Form.Item name="note" label="备注" style={{ minWidth: 260, flex: 1 }}>
              <Input.TextArea rows={1} maxLength={400} />
            </Form.Item>
          </Flex>
        </Form>
      </Modal>
    </div>
  );
}

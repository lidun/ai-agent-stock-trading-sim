import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Empty,
  Flex,
  Modal,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  confirmRetroReport,
  dispatchKbReferenceCards,
  extractRetroReport,
  fetchKbEvidenceEta,
  listAgents,
  listKb,
  listKbCandidates,
  listKbFreeTags,
  listKbTagAliases,
  listRetroReports,
  mergeKbTag,
  transitionKb,
  type AgentInfo,
  type KbCandidate,
  type KbEvidenceBucket,
  type KbEntry,
  type KbReferenceCard,
  type KbTagAlias,
  type KbTagProposal,
  type KbStatus,
  type RetroReport,
} from "../../api/endpoints";

const STATUS_META: Record<KbStatus, { color: string; text: string }> = {
  observing: { color: "default", text: "观察中" },
  validating: { color: "blue", text: "验证中" },
  valid: { color: "green", text: "有效" },
  invalid: { color: "red", text: "已失效" },
  sealed: { color: "gold", text: "已封存" },
};

function num(v: number | null | undefined, digits = 2): string {
  return v == null ? "—" : v.toFixed(digits);
}
function pct(v: number | null | undefined): string {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}

interface Props {
  onChanged: () => void;
}

/**
 * 概念治理区（spec-06 §6.7 v0.3 A5）：状态机数据结论候选（spec-05 §3.2/§3.3）、
 * 概念验证进度外推（§3.2 B3）、月度标签归并确认队列（§3.11）与参考卡下发（§3.5/§3.9）。
 * 所有迁移/归并均为写操作，经本组件提交 core API 审计留痕。
 */
export default function ConceptGovernance({ onChanged }: Props) {
  const { message, modal } = AntApp.useApp();
  const [candidates, setCandidates] = useState<KbCandidate[]>([]);
  const [insufficient, setInsufficient] = useState(0);
  const [eta, setEta] = useState<KbEvidenceBucket[]>([]);
  const [etaWindow, setEtaWindow] = useState(30);
  const [aliases, setAliases] = useState<KbTagAlias[]>([]);
  const [proposals, setProposals] = useState<KbTagProposal[]>([]);
  const [entries, setEntries] = useState<KbEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [cardOpen, setCardOpen] = useState(false);
  const [cards, setCards] = useState<KbReferenceCard[]>([]);
  const [reports, setReports] = useState<RetroReport[]>([]);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [retroAgent, setRetroAgent] = useState("");
  const [detail, setDetail] = useState<RetroReport | null>(null);

  const positiveEntries = useMemo(
    () => entries.filter((e) => e.type === "positive" && !e.deleted_ts),
    [entries],
  );
  const entryName = useMemo(() => {
    const m = new Map<string, string>();
    entries.forEach((e) => m.set(e.id, e.name));
    return m;
  }, [entries]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [c, ev, tags, list, retro, ag] = await Promise.all([
        listKbCandidates(),
        fetchKbEvidenceEta({ window_days: etaWindow }),
        listKbFreeTags({ min_signals: 1, limit: 50 }),
        listKb({ includeDeleted: false }),
        listRetroReports({ limit: 50 }),
        listAgents(),
      ]);
      setCandidates(c.candidates);
      setInsufficient(c.insufficient_buckets);
      setEta(ev.buckets);
      setProposals(tags.proposals);
      setEntries(list.entries);
      setReports(retro.reports);
      setAgents(ag.agents);
      setRetroAgent((prev) => prev || ag.agents.find((a) => a.role === "strategy")?.id || "");
      const al = await listKbTagAliases();
      setAliases(al.aliases);
    } catch (e) {
      message.error((e as Error).message ?? "概念治理数据加载失败");
    } finally {
      setLoading(false);
    }
  }, [etaWindow, message]);

  useEffect(() => {
    void load();
  }, [load]);

  const doTransition = (c: KbCandidate) => {
    const label = c.action === "promote_valid" ? "确认有效" : "判定失效";
    const action = c.action === "promote_valid" ? "approve_valid" : "invalidate";
    modal.confirm({
      title: `${label}？`,
      content: (
        <div style={{ fontSize: 13 }}>
          <p style={{ marginBottom: 6 }}>{c.name}（{c.kb_id}）·桶 {c.env_bucket}</p>
          <p style={{ marginBottom: 0, color: "#888" }}>{c.reason}</p>
        </div>
      ),
      okText: label,
      okButtonProps: { danger: action === "invalidate" },
      onOk: async () => {
        await transitionKb(c.kb_id, action, `数据结论候选：${c.reason}`);
        message.success(`${c.kb_id} 已${label}`);
        onChanged();
        void load();
      },
    });
  };

  const doMerge = async (p: KbTagProposal, canonicalKbId: string) => {
    setBusy(true);
    try {
      const r = await mergeKbTag({
        alias: p.alias,
        canonical_kb_id: canonicalKbId,
        reason: `月度归并：自由标签「${p.alias}」→ ${entryName.get(canonicalKbId) ?? canonicalKbId}`,
      });
      message.success(r.created ? "归并已生效（统计将按规范名重算）" : "该映射已存在");
      onChanged();
      void load();
    } catch (e) {
      message.error((e as Error).message ?? "归并失败");
    } finally {
      setBusy(false);
    }
  };

  const doDispatch = async () => {
    setBusy(true);
    try {
      const r = await dispatchKbReferenceCards({ limit: 8 });
      setCards(r.cards);
      setCardOpen(true);
      onChanged();
      void load();
    } catch (e) {
      message.error((e as Error).message ?? "参考卡下发失败");
    } finally {
      setBusy(false);
    }
  };

  const doExtract = async () => {
    if (!retroAgent) {
      message.warning("请选择要提取经验的 Agent");
      return;
    }
    setBusy(true);
    try {
      const r = await extractRetroReport({ agent_id: retroAgent });
      setDetail(r.report);
      message.success("终局归因报告已生成（待管理评审）");
      void load();
    } catch (e) {
      message.error((e as Error).message ?? "经验提取失败");
    } finally {
      setBusy(false);
    }
  };

  const doConfirmRetro = async (rpt: RetroReport) => {
    setBusy(true);
    try {
      const r = await confirmRetroReport(rpt.id, { note: "前端评审确认" });
      setDetail(r.report);
      message.success("报告已确认，评审留痕");
      void load();
    } catch (e) {
      message.error((e as Error).message ?? "确认失败");
    } finally {
      setBusy(false);
    }
  };

  const candidateCols: ColumnsType<KbCandidate> = [
    {
      title: "条目", key: "entry", width: 220,
      render: (_, c) => (
        <Flex vertical gap={2}>
          <Typography.Text strong>{c.name}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {c.kb_id} · {c.type === "pitfall" ? "避坑" : "正向"}
          </Typography.Text>
        </Flex>
      ),
    },
    {
      title: "状态", dataIndex: "status", width: 90,
      render: (s: KbStatus) => <Tag color={STATUS_META[s].color}>{STATUS_META[s].text}</Tag>,
    },
    { title: "环境桶", dataIndex: "env_bucket", width: 110 },
    {
      title: "结论", dataIndex: "action", width: 100,
      render: (a: KbCandidate["action"]) =>
        a === "promote_valid" ? <Tag color="green">建议晋升</Tag> : <Tag color="red">建议失效</Tag>,
    },
    {
      title: "统计", key: "stat", width: 220,
      render: (_, c) => (
        <Space size={8} wrap style={{ fontSize: 12 }}>
          <span>n={c.sample_n}</span>
          <span>胜率 {pct(c.win_rate)}</span>
          <span>E={num(c.expectancy)}</span>
          <Tooltip title={`滚动 ${c.rolling_n} 样本期望值`}>
            <span style={{ color: (c.rolling_expectancy ?? 0) < 0 ? "#cf1322" : undefined }}>
              滚动E={num(c.rolling_expectancy)}
            </span>
          </Tooltip>
        </Space>
      ),
    },
    {
      title: "依据", dataIndex: "reason", ellipsis: true,
      render: (s: string) => <Typography.Text style={{ fontSize: 12 }}>{s}</Typography.Text>,
    },
    {
      title: "操作", key: "op", width: 100,
      render: (_, c) => (
        <Button
          size="small"
          type={c.action === "promote_valid" ? "primary" : "default"}
          danger={c.action === "invalidate"}
          onClick={() => doTransition(c)}
        >
          {c.action === "promote_valid" ? "确认有效" : "判定失效"}
        </Button>
      ),
    },
  ];

  const etaCols: ColumnsType<KbEvidenceBucket> = [
    {
      title: "条目", key: "entry", width: 220,
      render: (_, b) => (
        <Flex vertical gap={2}>
          <Typography.Text strong>{b.name}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {b.kb_id} · 桶 {b.env_bucket}
          </Typography.Text>
        </Flex>
      ),
    },
    {
      title: "状态", dataIndex: "status", width: 90,
      render: (s: KbStatus) => <Tag color={STATUS_META[s].color}>{STATUS_META[s].text}</Tag>,
    },
    {
      title: "样本", key: "n", width: 130,
      render: (_, b) => (
        <span>{b.sample_n} / {b.min_n}</span>
      ),
    },
    {
      title: "近窗频率", key: "freq", width: 130,
      render: (_, b) => <span>{b.recent_n} 次 · {num(b.freq, 4)}/日</span>,
    },
    {
      title: "预计达门槛", key: "eta", width: 150,
      render: (_, b) =>
        b.eta_days === 0 ? (
          <Tag color="green">样本充足</Tag>
        ) : b.eta_days == null ? (
          <Typography.Text type="secondary">无法外推</Typography.Text>
        ) : (
          <Tag color="blue">约 {b.eta_days} 交易日</Tag>
        ),
    },
    {
      title: "说明", dataIndex: "reason", ellipsis: true,
      render: (s: string) => <Typography.Text style={{ fontSize: 12 }}>{s}</Typography.Text>,
    },
  ];

  const proposalCols: ColumnsType<KbTagProposal> = [
    {
      title: "自由标签", dataIndex: "alias", width: 180,
      render: (s: string) => <Typography.Text code>{s}</Typography.Text>,
    },
    { title: "信号数", dataIndex: "signals", width: 80 },
    {
      title: "建议规范条目", key: "suggest", width: 260,
      render: (_, p) => (
        <Space size={6}>
          <Typography.Link onClick={() => undefined}>{p.suggested_name}</Typography.Link>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {p.suggested_kb_id} · 相似度 {num(p.score, 3)}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "确认归并到", key: "merge", width: 260,
      render: (_, p) => <MergeCell proposal={p} entries={positiveEntries} busy={busy} onMerge={doMerge} />,
    },
  ];

  const retroCols: ColumnsType<RetroReport> = [
    { title: "报告", dataIndex: "id", width: 160, render: (s: string) => <Typography.Text code>{s}</Typography.Text> },
    { title: "Agent", dataIndex: "agent_id", width: 150 },
    {
      title: "状态", dataIndex: "status", width: 100,
      render: (s: RetroReport["status"]) =>
        s === "confirmed" ? <Tag color="green">已确认</Tag> : <Tag color="orange">待评审</Tag>,
    },
    {
      title: "归因", dataIndex: "attribution_status", width: 130,
      render: (s: string) =>
        s === "generated" ? <Tag color="blue">LLM</Tag> : <Tag color="default">{s || "—"}</Tag>,
    },
    {
      title: "有效/无效桶", key: "v", width: 120,
      render: (_, r) => <span>{r.stats.effective} / {r.stats.ineffective}</span>,
    },
    { title: "已结清样本", key: "n", width: 110, render: (_, r) => r.stats.settled_n },
    { title: "生成时间", dataIndex: "created_ts", width: 180 },
    {
      title: "操作", key: "op", width: 100,
      render: (_, r) => (
        <Button size="small" onClick={() => setDetail(r)}>查看</Button>
      ),
    },
  ];

  return (
    <Card
      size="small"
      title="概念治理（spec-05 §3.2/§3.11）"
      extra={
        <Space>
          <Button size="small" onClick={() => void load()} loading={loading}>
            刷新
          </Button>
          <Button size="small" type="primary" onClick={() => void doDispatch()} loading={busy}>
            下发参考卡
          </Button>
        </Space>
      }
      style={{ marginBottom: 12 }}
    >
      <Tabs
        size="small"
        items={[
          {
            key: "candidates",
            label: `晋升/失效候选（${candidates.length}）`,
            children: candidates.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={`暂无数据结论候选（证据不足桶 ${insufficient} 个；门槛 n≥30、滚动 60 交易日）`}
              />
            ) : (
              <>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 8 }}
                  message="以下为客观统计给出的候选结论：实际迁移由人工评审确认（写 review_gate2 留痕），统计不回写历史信号。"
                />
                <Table rowKey={(c) => `${c.kb_id}-${c.env_bucket}-${c.action}`} size="small" pagination={false} columns={candidateCols} dataSource={candidates} scroll={{ x: 980 }} />
              </>
            ),
          },
          {
            key: "eta",
            label: "验证进度",
            children: (
              <>
                <Flex justify="space-between" align="center" style={{ marginBottom: 8 }}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    预计交易日 = (门槛 n − 当前桶 n) ÷ 近窗入信号频率（trial 样本排除）
                  </Typography.Text>
                  <Select
                    size="small"
                    style={{ width: 150 }}
                    value={etaWindow}
                    onChange={setEtaWindow}
                    options={[
                      { value: 10, label: "近 10 交易日" },
                      { value: 30, label: "近 30 交易日" },
                      { value: 60, label: "近 60 交易日" },
                    ]}
                  />
                </Flex>
                {eta.length === 0 ? (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无条目验证进度" />
                ) : (
                  <Table rowKey={(b) => `${b.kb_id}-${b.env_bucket}`} size="small" pagination={false} columns={etaCols} dataSource={eta} scroll={{ x: 900 }} />
                )}
              </>
            ),
          },
          {
            key: "tags",
            label: `标签归并确认（${proposals.length}）`,
            children: (
              <>
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginBottom: 8 }}
                  message="月度归并窗口（每月首个周末）：确认后写 kb_tag_aliases（append-only），统计按规范名归并、不回写历史信号行。"
                />
                {proposals.length === 0 ? (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无待确认的归并提议" />
                ) : (
                  <Table rowKey={(p) => p.alias} size="small" pagination={false} columns={proposalCols} dataSource={proposals} scroll={{ x: 820 }} />
                )}
                <Typography.Title level={5} style={{ marginTop: 16 }}>
                  已生效映射（{aliases.length}）
                </Typography.Title>
                {aliases.length === 0 ? (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>暂无归并映射。</Typography.Text>
                ) : (
                  <Table
                    rowKey={(a) => a.alias}
                    size="small"
                    pagination={false}
                    dataSource={aliases}
                    columns={[
                      { title: "自由标签", dataIndex: "alias", width: 180, render: (s: string) => <Typography.Text code>{s}</Typography.Text> },
                      { title: "规范条目", dataIndex: "canonical_kb_id", width: 140, render: (s: string) => `${entryName.get(s) ?? "?"}（${s}）` },
                      { title: "确认人", dataIndex: "merged_by", width: 120 },
                      { title: "归并时间", dataIndex: "merged_ts", width: 180 },
                      { title: "原因", dataIndex: "reason", ellipsis: true },
                    ]}
                  />
                )}
              </>
            ),
          },
          {
            key: "retro",
            label: `归档经验提取（${reports.length}）`,
            children: (
              <>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 8 }}
                  message="Agent 归档/退休时执行终局统计（确定性）→ LLM 归因 → 管理评审确认后回填知识库；trial 样本排除，concept_tag 按归并映射归口。"
                />
                <Space style={{ marginBottom: 8 }}>
                  <Select
                    size="small"
                    style={{ width: 220 }}
                    value={retroAgent || undefined}
                    onChange={setRetroAgent}
                    placeholder="选择 Agent"
                    options={agents.map((a) => ({
                      value: a.id,
                      label: `${a.name}（${a.id}）`,
                    }))}
                  />
                  <Button size="small" type="primary" loading={busy} onClick={() => void doExtract()}>
                    生成终局归因报告
                  </Button>
                </Space>
                {reports.length === 0 ? (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无归档经验提取报告" />
                ) : (
                  <Table rowKey={(r) => r.id} size="small" pagination={false} columns={retroCols} dataSource={reports} scroll={{ x: 1000 }} />
                )}
              </>
            ),
          },
        ]}
      />

      <Modal
        open={cardOpen}
        onCancel={() => setCardOpen(false)}
        footer={<Button onClick={() => setCardOpen(false)}>关闭</Button>}
        title="候选参考卡（参考非指令，多样并列）"
        width={760}
      >
        {cards.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无可下发参考卡" />
        ) : (
          <Space direction="vertical" style={{ width: "100%" }}>
            {cards.map((c) => (
              <Card key={`${c.kb_id}-${c.env_bucket}`} size="small">
                <Descriptions size="small" column={2} title={<Space><span>{c.name}</span><Tag color={STATUS_META[c.status].color}>{c.status_label}</Tag><Tag>{c.env_bucket}</Tag></Space>}>
                  <Descriptions.Item label="KB-id">{c.kb_id}</Descriptions.Item>
                  <Descriptions.Item label="类型">{c.type_label}</Descriptions.Item>
                  <Descriptions.Item label="样本 n">{c.sample_n}</Descriptions.Item>
                  <Descriptions.Item label="期望值（含费）">{num(c.expectancy, 4)}</Descriptions.Item>
                  <Descriptions.Item label="下发次数">{c.dispatch_n}</Descriptions.Item>
                  <Descriptions.Item label="UCB 分">{c.score == null ? "优先探索" : num(c.score, 4)}</Descriptions.Item>
                </Descriptions>
              </Card>
            ))}
          </Space>
        )}
      </Modal>

      <Modal
        open={detail != null}
        onCancel={() => setDetail(null)}
        width={820}
        title={detail ? `归档经验提取报告 · ${detail.id}` : ""}
        footer={
          <Space>
            <Button onClick={() => setDetail(null)}>关闭</Button>
            {detail?.status === "pending_review" && (
              <Button type="primary" loading={busy} onClick={() => detail && void doConfirmRetro(detail)}>
                评审确认
              </Button>
            )}
          </Space>
        }
      >
        {detail && (
          <Space direction="vertical" style={{ width: "100%" }} size={12}>
            <Descriptions size="small" column={3}>
              <Descriptions.Item label="Agent">{detail.agent_id}</Descriptions.Item>
              <Descriptions.Item label="状态">
                {detail.status === "confirmed" ? <Tag color="green">已确认</Tag> : <Tag color="orange">待评审</Tag>}
              </Descriptions.Item>
              <Descriptions.Item label="归因来源">{detail.attribution_status || "—"}</Descriptions.Item>
              <Descriptions.Item label="已结清样本">{detail.stats.settled_n}</Descriptions.Item>
              <Descriptions.Item label="有效桶">{detail.stats.effective}</Descriptions.Item>
              <Descriptions.Item label="无效桶">{detail.stats.ineffective}</Descriptions.Item>
            </Descriptions>
            <div>
              <Typography.Title level={5}>终局统计（概念 × 环境桶）</Typography.Title>
              {detail.stats.buckets.length === 0 ? (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>暂无已结清信号样本。</Typography.Text>
              ) : (
                <Table
                  rowKey={(b) => `${b.concept_tag}-${b.env_bucket}`}
                  size="small"
                  pagination={false}
                  dataSource={detail.stats.buckets}
                  columns={[
                    { title: "概念", dataIndex: "concept_tag", width: 160 },
                    { title: "环境桶", dataIndex: "env_bucket", width: 110 },
                    {
                      title: "结论", dataIndex: "verdict", width: 90,
                      render: (v: string) =>
                        v === "effective" ? <Tag color="green">有效</Tag>
                          : v === "ineffective" ? <Tag color="red">无效</Tag>
                            : <Tag>中性</Tag>,
                    },
                    { title: "n", dataIndex: "sample_n", width: 60 },
                    { title: "胜率", key: "wr", width: 80, render: (_, b) => pct(b.win_rate) },
                    { title: "E", key: "e", width: 80, render: (_, b) => num(b.expectancy) },
                  ]}
                />
              )}
            </div>
            <div>
              <Typography.Title level={5}>LLM 终局归因</Typography.Title>
              {detail.attribution ? (
                <Typography.Paragraph style={{ whiteSpace: "pre-wrap", fontSize: 13, marginBottom: 0 }}>
                  {detail.attribution}
                </Typography.Paragraph>
              ) : (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {detail.attribution_status === "not_configured"
                    ? "模型服务未配置，仅保留确定性终局统计；配置后可重新生成。"
                    : `未生成归因（${detail.attribution_status || "—"}）。`}
                </Typography.Text>
              )}
            </div>
            {detail.review_ref.ts && (
              <div>
                <Typography.Title level={5}>评审留痕</Typography.Title>
                <Typography.Text style={{ fontSize: 12 }}>
                  {detail.review_ref.decided_by} · {detail.review_ref.ts}
                  {detail.review_ref.note ? ` · ${detail.review_ref.note}` : ""}
                  {detail.review_ref.applied?.length
                    ? ` · 回填 ${detail.review_ref.applied.length} 项`
                    : ""}
                </Typography.Text>
              </div>
            )}
          </Space>
        )}
      </Modal>
    </Card>
  );
}

function MergeCell({
  proposal,
  entries,
  busy,
  onMerge,
}: {
  proposal: KbTagProposal;
  entries: KbEntry[];
  busy: boolean;
  onMerge: (p: KbTagProposal, canonicalKbId: string) => void;
}) {
  const [target, setTarget] = useState(proposal.suggested_kb_id);
  useEffect(() => {
    setTarget(proposal.suggested_kb_id);
  }, [proposal.suggested_kb_id]);
  return (
    <Space size={6}>
      <Select
        size="small"
        style={{ width: 150 }}
        value={target}
        onChange={setTarget}
        options={entries.map((e) => ({ value: e.id, label: `${e.name}（${e.id}）` }))}
      />
      <Button size="small" type="primary" loading={busy} onClick={() => onMerge(proposal, target)}>
        确认归并
      </Button>
    </Space>
  );
}

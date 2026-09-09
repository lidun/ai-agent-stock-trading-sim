import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Empty,
  Flex,
  Select,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  fetchCharterVersion,
  fetchStrategyProfile,
  listAgents,
  type AgentInfo,
  type CharterFull,
  type CharterVersionSummary,
  type StrategyProfile,
} from "../../api/endpoints";
import { fmtBeijingTime } from "../../utils/time";

type DiffLine = { kind: "add" | "del" | "same"; text: string };

/** 行级 diff（LCS），用于章程正文版本对比（spec-06 §6.9 变更摘要）。 */
function lineDiff(base: string, target: string): DiffLine[] {
  const a = (base || "").split("\n");
  const b = (target || "").split("\n");
  const n = a.length;
  const m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ kind: "same", text: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ kind: "del", text: a[i] });
      i++;
    } else {
      out.push({ kind: "add", text: b[j] });
      j++;
    }
  }
  while (i < n) out.push({ kind: "del", text: a[i++] });
  while (j < m) out.push({ kind: "add", text: b[j++] });
  return out;
}

const LINE_STYLE: Record<DiffLine["kind"], { color: string; bg: string }> = {
  add: { color: "#389e0d", bg: "rgba(56,158,13,0.08)" },
  del: { color: "#cf1322", bg: "rgba(207,19,34,0.07)" },
  same: { color: "rgba(0,0,0,0.65)", bg: "transparent" },
};

/** 双层结构差异：新增/删除/变化的顶层键。 */
function layerDelta(base: Record<string, unknown>, target: Record<string, unknown>) {
  const baseKeys = Object.keys(base ?? {});
  const targetKeys = Object.keys(target ?? {});
  const added = targetKeys.filter((k) => !(k in (base ?? {})));
  const removed = baseKeys.filter((k) => !(k in (target ?? {})));
  const changed = targetKeys.filter(
    (k) => k in (base ?? {}) && JSON.stringify((base ?? {})[k]) !== JSON.stringify((target ?? {})[k]),
  );
  return { added, removed, changed };
}

export default function CharterManagePage() {
  const { message } = AntApp.useApp();
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [agentId, setAgentId] = useState("");
  const [profile, setProfile] = useState<StrategyProfile | null>(null);
  const [loading, setLoading] = useState(false);

  const [expanded, setExpanded] = useState<string | null>(null);
  const [details, setDetails] = useState<Record<string, CharterFull>>({});
  const [detailLoading, setDetailLoading] = useState(false);
  const [baseNo, setBaseNo] = useState("");
  const [targetNo, setTargetNo] = useState("");

  useEffect(() => {
    listAgents()
      .then((r) => {
        const strategy = r.agents.filter((a) => a.role === "strategy");
        setAgents(strategy);
        if (strategy.length > 0) setAgentId(strategy[0].id);
      })
      .catch((e) => message.error((e as Error).message ?? "加载 Agent 失败"));
  }, [message]);

  const loadProfile = useCallback(
    async (id: string) => {
      if (!id) return;
      setLoading(true);
      setDetails({});
      setExpanded(null);
      try {
        const p = await fetchStrategyProfile(id);
        setProfile(p);
        const nums = p.versions.map((v) => v.version_no);
        setBaseNo(nums[0] ?? "");
        setTargetNo(p.versions.find((v) => v.active)?.version_no ?? nums[nums.length - 1] ?? "");
      } catch (e) {
        message.error((e as Error).message ?? "加载章程失败");
      } finally {
        setLoading(false);
      }
    },
    [message],
  );

  useEffect(() => {
    void loadProfile(agentId);
  }, [agentId, loadProfile]);

  const ensureDetail = useCallback(
    async (versionNo: string) => {
      if (!agentId || !versionNo) return null;
      if (details[versionNo]) return details[versionNo];
      setDetailLoading(true);
      try {
        const d = await fetchCharterVersion(agentId, versionNo);
        setDetails((prev) => ({ ...prev, [versionNo]: d.version }));
        return d.version;
      } catch (e) {
        message.error((e as Error).message ?? "加载版本详情失败");
        return null;
      } finally {
        setDetailLoading(false);
      }
    },
    [agentId, details, message],
  );

  useEffect(() => {
    if (baseNo && targetNo && baseNo !== targetNo) {
      void ensureDetail(baseNo);
      void ensureDetail(targetNo);
    }
  }, [baseNo, targetNo, ensureDetail]);

  const compare = useMemo(() => {
    if (!baseNo || !targetNo || baseNo === targetNo) return null;
    const base = details[baseNo];
    const target = details[targetNo];
    if (!base || !target) return null;
    const beliefDiff = lineDiff(base.core_belief, target.core_belief);
    const layerDiff = layerDelta(base.layers, target.layers);
    return { base, target, beliefDiff, layerDiff };
  }, [baseNo, targetNo, details]);

  const versionColumns: ColumnsType<CharterVersionSummary> = [
    {
      title: "版本", dataIndex: "version_no", width: 110,
      render: (v: string) => <Typography.Text code>{v}</Typography.Text>,
    },
    {
      title: "状态", dataIndex: "active", width: 90,
      render: (active: boolean) =>
        active ? <Tag color="green">当前生效</Tag> : <Tag color="default">历史</Tag>,
    },
    {
      title: "锁定", dataIndex: "locked", width: 80,
      render: (locked: boolean) => (locked ? <Tag color="blue">核心理念锁定</Tag> : <Tag>未锁定</Tag>),
    },
    { title: "变更说明", dataIndex: "note", ellipsis: true, render: (v: string) => v || "—" },
    {
      title: "创建时间", dataIndex: "created_ts", width: 160,
      render: (v: string) => fmtBeijingTime(v),
    },
    {
      title: "charter_hash", dataIndex: "charter_hash", width: 130,
      render: (v: string) =>
        v ? <Typography.Text code style={{ fontSize: 11 }}>{v.slice(0, 12)}</Typography.Text> : "—",
    },
  ];

  const agentOptions = agents.map((a) => ({ value: a.id, label: `${a.name}（${a.id}）` }));

  return (
    <div style={{ padding: 16, minHeight: "100%" }}>
      <Flex justify="space-between" align="flex-start" gap={12} wrap style={{ marginBottom: 12 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            大纲管理
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            策略章程版本链查看与对比（spec-06 §6.9 / spec-05 §7.1 charter_versions；变更锁定=locked，写入口走管理 Agent 授权）
          </Typography.Text>
        </div>
        <Select
          style={{ width: 320 }}
          placeholder="选择策略 Agent"
          value={agentId || undefined}
          options={agentOptions}
          onChange={(v) => setAgentId(v)}
        />
      </Flex>

      {loading && !profile && <Skeleton active paragraph={{ rows: 8 }} />}

      {!loading && agents.length === 0 && (
        <Card><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无策略子 Agent" /></Card>
      )}

      {!loading && profile && profile.versions.length === 0 && (
        <Alert type="info" showIcon message={`${agentId} 尚无章程落库`} description="管理 Agent 生成策略理念后在此形成版本链（spec-05 §7.1）。" />
      )}

      {!loading && profile && profile.versions.length > 0 && (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Card size="small" title="当前生效章程（核心理念 · active）">
            {profile.active ? (
              <>
                <Flex wrap gap={8} style={{ marginBottom: 8 }}>
                  <Tag color="purple">{profile.active.version_no}</Tag>
                  <Tag color={profile.active.locked ? "blue" : "default"}>
                    {profile.active.locked ? "核心理念锁定（变更需授权）" : "未锁定"}
                  </Tag>
                  {profile.active.charter_hash && (
                    <Typography.Text code style={{ fontSize: 11 }}>
                      {profile.active.charter_hash}
                    </Typography.Text>
                  )}
                </Flex>
                <Typography.Paragraph style={{ whiteSpace: "pre-wrap" }}>
                  {profile.active.core_belief || "（无理念正文）"}
                </Typography.Paragraph>
                {profile.active.note && (
                  <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
                    变更说明：{profile.active.note}
                  </Typography.Paragraph>
                )}
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  双层结构快照（spec-05 #21）：
                </Typography.Text>
                <pre style={{ margin: "4px 0 0", fontSize: 12, whiteSpace: "pre-wrap", background: "rgba(0,0,0,0.03)", padding: 8, borderRadius: 4 }}>
                  {JSON.stringify(profile.active.layers ?? {}, null, 2)}
                </pre>
              </>
            ) : (
              <Typography.Text type="secondary">无 active 版本（数据异常）</Typography.Text>
            )}
          </Card>

          <Card
            size="small"
            title="版本链与版本对比"
            extra={<Typography.Text type="secondary" style={{ fontSize: 12 }}>{profile.versions.length} 个版本 · 展开行查看正文</Typography.Text>}
          >
            <Flex gap={8} wrap align="center" style={{ marginBottom: 10 }}>
              <span style={{ fontSize: 13 }}>对比基线</span>
              <Select size="small" style={{ width: 130 }} value={baseNo} options={profile.versions.map((v) => ({ value: v.version_no, label: v.version_no }))} onChange={setBaseNo} />
              <span style={{ fontSize: 13 }}>→ 目标</span>
              <Select size="small" style={{ width: 130 }} value={targetNo} options={profile.versions.map((v) => ({ value: v.version_no, label: v.version_no }))} onChange={setTargetNo} />
              {(baseNo || targetNo) && (
                <Button size="small" type="link" onClick={() => {
                  const nums = profile.versions.map((v) => v.version_no);
                  setBaseNo(nums[0] ?? "");
                  setTargetNo(profile.versions.find((v) => v.active)?.version_no ?? nums[nums.length - 1] ?? "");
                }}>
                  复位默认
                </Button>
              )}
            </Flex>

            {detailLoading && <Skeleton active paragraph={{ rows: 3 }} />}
            {compare && !detailLoading && (
              <Card size="small" type="inner" title={`变更摘要 ${baseNo} → ${targetNo}`} style={{ marginBottom: 12 }}>
                {compare.target.note && (
                  <Alert type="info" showIcon style={{ marginBottom: 8 }} message={`目标版本变更说明：${compare.target.note}`} />
                )}
                <Typography.Text strong style={{ fontSize: 12 }}>核心理念正文 diff</Typography.Text>
                <div style={{ margin: "4px 0 10px", maxHeight: 240, overflow: "auto", background: "rgba(0,0,0,0.02)", borderRadius: 4, padding: 4 }}>
                  {compare.beliefDiff.every((l) => l.kind === "same")
                    ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>正文无变化</Typography.Text>
                    : compare.beliefDiff.map((l, idx) => (
                        <div key={idx} style={{ fontSize: 12, color: LINE_STYLE[l.kind].color, background: LINE_STYLE[l.kind].bg, whiteSpace: "pre-wrap" }}>
                          {l.kind === "add" ? "+ " : l.kind === "del" ? "- " : "  "}
                          {l.text || "（空行）"}
                        </div>
                      ))}
                </div>
                <Typography.Text strong style={{ fontSize: 12 }}>双层结构层键差异</Typography.Text>
                <div style={{ marginTop: 4 }}>
                  <Space size={6} wrap>
                    {compare.layerDiff.added.map((k) => <Tag key={k} color="green">新增：{k}</Tag>)}
                    {compare.layerDiff.removed.map((k) => <Tag key={k} color="red">删除：{k}</Tag>)}
                    {compare.layerDiff.changed.map((k) => <Tag key={k} color="orange">变化：{k}</Tag>)}
                    {compare.layerDiff.added.length + compare.layerDiff.removed.length + compare.layerDiff.changed.length === 0 && (
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>层结构无变化</Typography.Text>
                    )}
                  </Space>
                </div>
              </Card>
            )}
            {baseNo && targetNo && baseNo === targetNo && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>选择两个不同版本查看 diff。</Typography.Text>
            )}

            <Table<CharterVersionSummary>
              rowKey="version_no"
              size="small"
              columns={versionColumns}
              dataSource={profile.versions}
              pagination={false}
              scroll={{ x: 860 }}
              expandable={{
                expandedRowKeys: expanded ? [expanded] : [],
                onExpand: (open, row) => {
                  const next = open ? row.version_no : null;
                  setExpanded(next);
                  if (next) void ensureDetail(next);
                },
                expandedRowRender: (row) => {
                  const d = details[row.version_no];
                  if (!d) return <Skeleton active paragraph={{ rows: 3 }} />;
                  return (
                    <div style={{ maxWidth: 820 }}>
                      <Typography.Paragraph style={{ whiteSpace: "pre-wrap", margin: 0 }}>{d.core_belief || "（无理念正文）"}</Typography.Paragraph>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>双层结构：</Typography.Text>
                      <pre style={{ margin: 0, fontSize: 11, whiteSpace: "pre-wrap" }}>{JSON.stringify(d.layers ?? {}, null, 2)}</pre>
                    </div>
                  );
                },
              }}
            />
          </Card>

          <Card size="small" title="当前值（供外部联动）">
            <Descriptions size="small" column={3} items={[
              { key: "agent", label: "Agent", children: profile.agent_id },
              { key: "active", label: "active 版本", children: profile.versions.find((v) => v.active)?.version_no ?? "—" },
              { key: "versions", label: "版本数", children: profile.versions.length },
              { key: "capability", label: "能力包绑定", children: profile.has_capability_packs ? `${profile.capability_packs.length} 项` : "无" },
            ]} />
          </Card>
        </Space>
      )}
    </div>
  );
}

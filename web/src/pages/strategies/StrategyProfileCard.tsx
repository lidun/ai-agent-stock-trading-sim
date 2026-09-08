import { useEffect, useState } from "react";
import {
  Card,
  Empty,
  Flex,
  Skeleton,
  Table,
  Tag,
  Typography,
} from "antd";
import { LockOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import {
  fetchCharterVersion,
  type CharterVersionDetail,
  type CharterVersionSummary,
  type StrategyProfile,
} from "../../api/endpoints";
import { fmtBeijing } from "../../utils/time";

interface Props {
  profile: StrategyProfile | null;
  loading: boolean;
}

const versionColumns: ColumnsType<CharterVersionSummary> = [
  {
    title: "版本", dataIndex: "version_no", width: 110,
    render: (v: string) => <Typography.Text code>{v}</Typography.Text>,
  },
  {
    title: "状态", dataIndex: "active", width: 90,
    render: (a: boolean) => (a ? <Tag color="blue">现役</Tag> : <Tag>历史</Tag>),
  },
  {
    title: "理念锁定", dataIndex: "locked", width: 100,
    render: (l: boolean) =>
      l ? <Tag color="red" icon={<LockOutlined />}>锁定</Tag> : <Tag>未锁定</Tag>,
  },
  { title: "说明", dataIndex: "note", ellipsis: true, render: (v: string) => v || "—" },
  {
    title: "理念哈希", dataIndex: "charter_hash", width: 140,
    render: (v: string) =>
      v ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }} code>
          {v.slice(0, 12)}
        </Typography.Text>
      ) : (
        <Typography.Text type="secondary">—</Typography.Text>
      ),
  },
  { title: "建立", dataIndex: "created_ts", width: 150, render: (v: string) => fmtBeijing(v) },
];

export default function StrategyProfileCard({ profile, loading }: Props) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<CharterVersionDetail | null>(null);

  const active = profile?.active ?? null;

  useEffect(() => {
    if (!expanded || !profile) {
      setDetail(null);
      return;
    }
    let live = true;
    setDetail(null);
    void fetchCharterVersion(profile.agent_id, expanded)
      .then((d) => {
        if (live) setDetail(d);
      })
      .catch(() => {
        if (live) setDetail(null);
      });
    return () => {
      live = false;
    };
  }, [expanded, profile]);

  return (
    <Card size="small" style={{ marginBottom: 12 }} title="策略理念 · 能力包">
      {loading ? (
        <Skeleton active paragraph={{ rows: 4 }} />
      ) : !profile || !active ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              暂无策略章程落库 —— 理念由策略发布/管理 Agent 写入后展示（spec-05 §4.1 双层结构 +
              锁定语义）；能力包绑定待 spec-05 §5 能力配置中心落地。写入口由后续切片提供。
            </Typography.Text>
          }
          style={{ padding: "8px 0" }}
        />
      ) : (
        <>
          <Flex gap={8} wrap align="center" style={{ marginBottom: 8 }}>
            <Tag color="blue">{active.version_no}</Tag>
            {active.locked ? (
              <Tag color="red" icon={<LockOutlined />}>
                核心理念已锁定（变更需用户授权，spec-05 §4.1）
              </Tag>
            ) : (
              <Tag>理念未锁定</Tag>
            )}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              建立于 {fmtBeijing(active.created_ts)}
            </Typography.Text>
          </Flex>
          <div
            style={{
              borderLeft: "3px solid #cf1322",
              background: "rgba(0,0,0,0.03)",
              padding: "8px 12px",
              borderRadius: 4,
              marginBottom: 8,
            }}
          >
            <Typography.Paragraph
              style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 13 }}
            >
              {active.core_belief || "（该版本未记录理念正文）"}
            </Typography.Paragraph>
          </div>
          {active.note && (
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
              变更说明：{active.note}
            </Typography.Paragraph>
          )}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            双层结构快照（spec-05 #21）：
          </Typography.Text>
          <pre
            style={{
              margin: "4px 0 8px",
              fontSize: 12,
              maxHeight: 160,
              overflow: "auto",
              background: "rgba(0,0,0,0.03)",
              padding: 8,
              borderRadius: 4,
            }}
          >
            {JSON.stringify(active.layers ?? {}, null, 2)}
          </pre>
          <Flex wrap gap={8} style={{ marginBottom: 8 }}>
            {profile.has_capability_packs ? (
              <>
                <Tag color="green">能力包：已绑定 {profile.capability_packs.length} 项</Tag>
                {profile.capability_packs.map((cp) => (
                  <Tag key={`${cp.name}@${cp.version}`} color="blue">
                    {cp.name}@{cp.version}
                  </Tag>
                ))}
              </>
            ) : (
              <Tag color="default">能力包：待 spec-05 §2 配置中心绑定</Tag>
            )}
          </Flex>
          {profile.versions.length > 1 && (
            <Table<CharterVersionSummary>
              rowKey="version_no"
              size="small"
              columns={versionColumns}
              dataSource={profile.versions}
              pagination={false}
              scroll={{ x: 720 }}
              expandable={{
                expandedRowKeys: expanded ? [expanded] : [],
                onExpand: (open, row) => setExpanded(open ? row.version_no : null),
                expandedRowRender: (row) =>
                  detail?.version.version_no === row.version_no ? (
                    <Flex vertical gap={4} style={{ maxWidth: 720 }}>
                      <Typography.Text style={{ fontSize: 13, whiteSpace: "pre-wrap" }}>
                        {detail.version.core_belief || "（无理念正文）"}
                      </Typography.Text>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        双层结构：{JSON.stringify(detail.version.layers ?? {})}
                      </Typography.Text>
                      {detail.version.note && (
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          说明：{detail.version.note}
                        </Typography.Text>
                      )}
                    </Flex>
                  ) : (
                    <Typography.Text type="secondary">加载中…</Typography.Text>
                  ),
              }}
            />
          )}
        </>
      )}
    </Card>
  );
}

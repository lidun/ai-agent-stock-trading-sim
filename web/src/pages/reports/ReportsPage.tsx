import { useCallback, useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Flex,
  Segmented,
  Select,
  Skeleton,
  Table,
  Tag,
  Typography,
  theme as antTheme,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import {
  fetchReportVersions,
  listAccounts,
  listReportTimeline,
  type AccountInfo,
  type ReportTimelineEntry,
  type ReportVersion,
} from "../../api/endpoints";
import { fmtBeijingTime } from "../../utils/time";

const REPORT_STATUS: Record<string, { color: string; text: string }> = {
  normal: { color: "green", text: "正常" },
  absent: { color: "orange", text: "缺勤" },
  resend: { color: "purple", text: "修订" },
};

interface TimelineRow extends ReportTimelineEntry {
  agent_id: string;
  agent_name: string;
}

function num(v: string | null | undefined): number {
  return Number(v ?? "0");
}

function money(v: string | null | undefined): string {
  return num(v).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/** 日报中心（spec-06 §6.6）：时间线（按 Agent/日期筛选）+ 版本切换阅读全文。
 *  merged_markdown 为引擎确定性渲染的数据段（spec-04 §5.2），absent/resend 版本切换查看。 */
export default function ReportsPage() {
  const { token } = antTheme.useToken();
  const { message } = AntApp.useApp();

  const [accounts, setAccounts] = useState<AccountInfo[]>([]);
  const [rows, setRows] = useState<TimelineRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [acctFilter, setAcctFilter] = useState<string>("all");

  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<{ agentId: string; date: string } | null>(null);
  const [versions, setVersions] = useState<ReportVersion[]>([]);
  const [verNo, setVerNo] = useState<number>(1);
  const [detailLoading, setDetailLoading] = useState(false);

  const accountName = useMemo(() => {
    const m = new Map<string, string>();
    accounts.forEach((a) => m.set(a.id, a.agent_name));
    return m;
  }, [accounts]);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const list = await listAccounts();
      setAccounts(list.accounts);
      const strategyAccs = list.accounts.filter((a) => a.agent_role === "strategy");
      if (acctFilter !== "all") {
        const target = strategyAccs.find((a) => a.agent_id === acctFilter);
        if (target) strategyAccs.splice(0, strategyAccs.length, target);
      }
      const views = await Promise.all(strategyAccs.map((a) => listReportTimeline(a.agent_id)));
      const merged: TimelineRow[] = views.flatMap((v, i) => {
        const acc = strategyAccs[i];
        return v.reports.map((r) => ({
          ...r,
          agent_id: acc.agent_id,
          agent_name: acc.agent_name,
        }));
      });
      merged.sort((a, b) =>
        a.trade_date === b.trade_date
          ? a.agent_name.localeCompare(b.agent_name)
          : b.trade_date.localeCompare(a.trade_date),
      );
      setRows(merged);
    } catch (e) {
      message.error((e as Error).message ?? "加载失败");
    } finally {
      setLoading(false);
    }
  }, [acctFilter, message]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const openDay = useCallback(async (agentId: string, date: string) => {
    setDetail({ agentId, date });
    setOpen(true);
    setDetailLoading(true);
    try {
      const body = await fetchReportVersions(agentId, date);
      setVersions(body.versions);
      setVerNo(body.versions[0]?.version ?? 1);
    } catch (e) {
      message.error((e as Error).message ?? "加载日报失败");
      setVersions([]);
    } finally {
      setDetailLoading(false);
    }
  }, [message]);

  const version = useMemo(
    () => versions.find((v) => v.version === verNo) ?? versions[0],
    [versions, verNo],
  );

  const columns: ColumnsType<TimelineRow> = [
    { title: "交易日", dataIndex: "trade_date", width: 120, sorter: (a, b) => b.trade_date.localeCompare(a.trade_date) },
    ...(acctFilter === "all"
      ? [{ title: "Agent", dataIndex: "agent_name", width: 140, render: (v: string) => v }]
      : []),
    {
      title: "状态",
      dataIndex: "status",
      width: 100,
      render: (s: string) => {
        const m = REPORT_STATUS[s] ?? { color: "default", text: s };
        return <Tag color={m.color}>{m.text}</Tag>;
      },
    },
    {
      title: "最新版本",
      dataIndex: "latest_version",
      width: 100,
      render: (v: number) => `v${v}`,
    },
    { title: "生成时间", dataIndex: "latest_created_ts", render: (v: string) => fmtBeijingTime(v) },
  ];

  const statusTag = (s: string) => {
    const m = REPORT_STATUS[s] ?? { color: "default", text: s };
    return <Tag color={m.color}>{m.text}</Tag>;
  };

  return (
    <div style={{ padding: 16, minHeight: "100%" }}>
      <Flex justify="space-between" align="flex-start" gap={12} wrap>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            日报中心
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            引擎数据段日报（spec-04 §5.2，结算零 token 直读生成）；缺勤/修订按版本切换查看；单篇导出见 spec-06 §6.6（P3）
          </Typography.Text>
        </div>
        <Flex gap={8} align="center">
          <Select
            style={{ width: 220 }}
            value={acctFilter}
            onChange={setAcctFilter}
            options={[
              { value: "all", label: "全部策略账户" },
              ...accounts.map((a) => ({ value: a.agent_id, label: a.agent_name })),
            ]}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void reload()}>
            刷新
          </Button>
        </Flex>
      </Flex>

      <Card size="small" title="日报时间线" style={{ marginTop: 12 }}>
        <Table<TimelineRow>
          rowKey={(r) => `${r.agent_id}:${r.trade_date}`}
          size="small"
          columns={columns}
          dataSource={rows}
          loading={loading}
          onRow={(r) => ({
            onClick: () => void openDay(r.agent_id, r.trade_date),
            style: { cursor: "pointer" },
          })}
          pagination={{ pageSize: 15, showSizeChanger: false, showTotal: (t) => `共 ${t} 期` }}
          locale={{
            emptyText: (
              <Empty
                description="尚无日报——EOD 结算成功后自动生成数据段首版（spec-04 §5.2）"
                style={{ padding: "24px 0" }}
              />
            ),
          }}
          scroll={{ x: 640 }}
        />
      </Card>

      <Drawer
        title={
          detail
            ? `${accountName.get(detail.agentId) ?? detail.agentId} · ${detail.date} 日报`
            : "日报"
        }
        width={Math.min(window.innerWidth - 48, 900)}
        open={open}
        onClose={() => setOpen(false)}
        extra={
          <Button icon={<ReloadOutlined />} onClick={() => detail && void openDay(detail.agentId, detail.date)}>
            刷新
          </Button>
        }
      >
        {detailLoading || !version ? (
          <Skeleton active paragraph={{ rows: 12 }} />
        ) : (
          <Flex vertical gap={12}>
            <Flex justify="space-between" align="center" wrap gap={8}>
              <Flex gap={8} align="center" wrap>
                {statusTag(version.status)}
                <Typography.Text type="secondary">创建于 {fmtBeijingTime(version.created_ts)}</Typography.Text>
              </Flex>
              {versions.length > 1 && (
                <Segmented
                  options={versions.map((v) => ({
                    label: `v${v.version} · ${REPORT_STATUS[v.status]?.text ?? v.status}`,
                    value: v.version,
                  }))}
                  value={verNo}
                  onChange={(v) => setVerNo(Number(v))}
                />
              )}
            </Flex>

            {version.data_section?.settlement && !version.data_section.settlement.done && (
              <Tag color="orange">当日结算产物缺失（缺勤日报：数据段为可得部分）</Tag>
            )}

            <Flex gap={8} wrap>
              <Card size="small" style={{ flex: 1, minWidth: 130 }} styles={{ body: { padding: "8px 12px" } }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  现金
                </Typography.Text>
                <div style={{ fontSize: 18, fontWeight: 600, color: token.colorText }}>
                  ¥{money(version.data_section?.summary?.cash)}
                </div>
              </Card>
              <Card size="small" style={{ flex: 1, minWidth: 130 }} styles={{ body: { padding: "8px 12px" } }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  份额法净值
                </Typography.Text>
                <div style={{ fontSize: 18, fontWeight: 600, color: token.colorText }}>
                  {num(version.data_section?.summary?.nav).toFixed(4)}
                </div>
              </Card>
              <Card size="small" style={{ flex: 1, minWidth: 130 }} styles={{ body: { padding: "8px 12px" } }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  当日盈亏
                </Typography.Text>
                <div
                  style={{
                    fontSize: 18,
                    fontWeight: 600,
                    color: num(version.data_section?.summary?.today_pnl) >= 0 ? "#cf1322" : "#3f8600",
                  }}
                >
                  {num(version.data_section?.summary?.today_pnl) >= 0 ? "+" : ""}
                  {money(version.data_section?.summary?.today_pnl)}
                </div>
              </Card>
              <Card size="small" style={{ flex: 1, minWidth: 130 }} styles={{ body: { padding: "8px 12px" } }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  累计盈亏
                </Typography.Text>
                <div style={{ fontSize: 18, fontWeight: 600, color: token.colorText }}>
                  {num(version.data_section?.summary?.total_pnl) >= 0 ? "+" : ""}
                  {money(version.data_section?.summary?.total_pnl)}
                </div>
              </Card>
            </Flex>

            {version.narrative ? (
              <Card size="small" title="叙述段">
                <div className="md-body">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{version.narrative}</ReactMarkdown>
                </div>
              </Card>
            ) : (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                叙述段待日报任务生成（LLM，spec-04 §5.2）
              </Typography.Text>
            )}

            <Card
              size="small"
              title="数据段（merged_markdown，确定性渲染）"
              extra={<Typography.Text type="secondary" style={{ fontSize: 12 }}>schema {version.data_section?.schema_version}</Typography.Text>}
              styles={{ body: { padding: "8px 16px" } }}
            >
              <div className="md-body">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{version.merged_markdown}</ReactMarkdown>
              </div>
            </Card>

            <Descriptions
              size="small"
              column={1}
              items={[
                {
                  key: "degraded",
                  label: "数据降级",
                  children:
                    version.data_section?.annotations?.degraded?.length ? (
                      <Flex gap={4} wrap>
                        {version.data_section.annotations.degraded.map((s) => (
                          <Tag key={s} color="orange">{s} L2 近似档</Tag>
                        ))}
                      </Flex>
                    ) : (
                      <Typography.Text type="secondary">无</Typography.Text>
                    ),
                },
              ]}
            />
          </Flex>
        )}
      </Drawer>
    </div>
  );
}

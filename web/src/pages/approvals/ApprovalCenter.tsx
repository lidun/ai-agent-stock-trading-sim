import { useCallback, useEffect, useMemo, useState } from "react";
import {
  App as AntApp,
  Button,
  Card,
  Empty,
  Flex,
  Input,
  Modal,
  Segmented,
  Select,
  Table,
  Tag,
  Typography,
  theme as antTheme,
} from "antd";
import {
  CheckOutlined,
  CloseOutlined,
  PlusOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import {
  decideApproval,
  listAccounts,
  listAgents,
  listApprovals,
  submitApproval,
  type AccountInfo,
  type AgentInfo,
  type ApprovalInfo,
} from "../../api/endpoints";
import { fmtBeijingTime } from "../../utils/time";

const APPROVAL_STATUS_COLOR: Record<string, { color: string; text: string }> = {
  pending: { color: "orange", text: "待决" },
  approved: { color: "green", text: "已通过" },
  rejected: { color: "red", text: "已驳回" },
  expired: { color: "default", text: "已过期" },
  withdrawn: { color: "purple", text: "已撤回" },
};

const APPROVAL_TYPE_COLOR: Record<string, { color: string; text: string }> = {
  exemption: { color: "blue", text: "豁免" },
  risk: { color: "volcano", text: "风险" },
  granularity: { color: "cyan", text: "撮合粒度" },
  capability: { color: "geekblue", text: "能力" },
  task: { color: "magenta", text: "任务" },
  launch: { color: "purple", text: "上线确认" },
};

/** 审批中心（spec-04 §4 审批流 / spec-06 §6.10）：人工待办+历史清单。
 *  管理 Agent LLM 未接入 → 决定方=登录用户；确定性短路（哈希去重/冷却/配额）由 core 判定。 */
export default function ApprovalCenterPage() {
  const { token } = antTheme.useToken();
  const { message, modal } = AntApp.useApp();

  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [accounts, setAccounts] = useState<AccountInfo[]>([]);
  const [rows, setRows] = useState<ApprovalInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>("all");

  const [createOpen, setCreateOpen] = useState(false);
  const [createAccount, setCreateAccount] = useState<string | undefined>();
  const [createTokens, setCreateTokens] = useState("");
  const [createReason, setCreateReason] = useState("");
  const [creating, setCreating] = useState(false);

  const [rejectTarget, setRejectTarget] = useState<ApprovalInfo | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [decidingId, setDecidingId] = useState<string | null>(null);

  const agentName = useMemo(() => {
    const m = new Map<string, string>();
    agents.forEach((a) => m.set(a.id, a.name));
    return m;
  }, [agents]);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [ag, acc, ap] = await Promise.all([
        listAgents(),
        listAccounts(),
        listApprovals({ status: statusFilter === "all" ? undefined : statusFilter }),
      ]);
      setAgents(ag.agents);
      setAccounts(acc.accounts);
      setRows(ap.approvals);
    } catch (e) {
      message.error((e as Error).message ?? "加载失败");
    } finally {
      setLoading(false);
    }
  }, [statusFilter, message]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const payloadSummary = (a: ApprovalInfo): string => {
    const p = a.payload ?? {};
    if (a.type === "exemption" && Array.isArray(p.tokens)) {
      return `豁免买入限制 token：${(p.tokens as string[]).join("、") || "（空）"}`;
    }
    const s = JSON.stringify(p);
    return s.length > 120 ? `${s.slice(0, 120)}…` : s;
  };

  const statusTag = (s: string) => {
    const m = APPROVAL_STATUS_COLOR[s] ?? { color: "default", text: s };
    return <Tag color={m.color}>{m.text}</Tag>;
  };

  const doDecide = useCallback(
    async (a: ApprovalInfo, decision: "approved" | "rejected", reason?: string) => {
      setDecidingId(a.id);
      try {
        const r = await decideApproval(a.id, decision, reason);
        if (!r.ok) {
          message.warning(r.detail ?? r.reason ?? "未生效（迟到/重复决定仅留痕）");
        } else {
          message.success(decision === "approved" ? "已通过并生效" : "已驳回");
        }
        await reload();
      } catch (e) {
        message.error((e as Error).message ?? "决定失败");
      } finally {
        setDecidingId(null);
      }
    },
    [message, reload],
  );

  const approve = (a: ApprovalInfo) => {
    modal.confirm({
      title: `通过审批单？`,
      content: `${a.type_label} · ${agentName.get(a.agent_id) ?? a.agent_id}：${payloadSummary(a)}`,
      okText: "确认通过",
      cancelText: "取消",
      onOk: () => doDecide(a, "approved"),
    });
  };

  const columns: ColumnsType<ApprovalInfo> = [
    { title: "提交时间", dataIndex: "created_ts", width: 150, render: (v: string) => fmtBeijingTime(v) },
    { title: "Agent", key: "agent", width: 150, render: (_, r) => agentName.get(r.agent_id) ?? r.agent_id },
    {
      title: "类型", dataIndex: "type", width: 100,
      render: (t: string) => {
        const m = APPROVAL_TYPE_COLOR[t] ?? { color: "default", text: t };
        return <Tag color={m.color}>{m.text}</Tag>;
      },
    },
    {
      title: "状态", dataIndex: "status", width: 100,
      render: (s: string) => statusTag(s),
      filters: [
        { text: "待决", value: "pending" },
        { text: "已通过", value: "approved" },
        { text: "已驳回", value: "rejected" },
        { text: "已过期", value: "expired" },
      ],
      onFilter: (v, r) => r.status === v,
    },
    {
      title: "申请内容", key: "body", width: 320,
      render: (_, r) => (
        <Flex vertical gap={2}>
          <Typography.Text>{payloadSummary(r)}</Typography.Text>
          {r.reason && r.status === "pending" && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              理由：{r.reason}
            </Typography.Text>
          )}
        </Flex>
      ),
    },
    {
      title: "决定 / 说明", key: "decide",
      render: (_, r) =>
        r.status === "pending" ? (
          <Flex gap={8} align="center">
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              过期 {fmtBeijingTime(r.expires_ts)}
            </Typography.Text>
            <Button
              size="small"
              type="primary"
              icon={<CheckOutlined />}
              loading={decidingId === r.id}
              onClick={() => approve(r)}
            >
              通过
            </Button>
            <Button size="small" danger icon={<CloseOutlined />} onClick={() => setRejectTarget(r)}>
              驳回
            </Button>
          </Flex>
        ) : (
          <Flex vertical gap={2}>
            {r.decided_ts && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {r.decided_by || "—"} · {fmtBeijingTime(r.decided_ts)}
              </Typography.Text>
            )}
            <Typography.Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: true }}>
              {r.reason || r.close_note || "—"}
            </Typography.Text>
          </Flex>
        ),
    },
  ];

  const createExemption = async () => {
    if (!createAccount) {
      message.warning("请选择目标账户");
      return;
    }
    const tokens = createTokens
      .split(/[,，\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (tokens.length === 0) {
      message.warning("请填写豁免 token（如 st、new，逗号分隔）");
      return;
    }
    if (!createReason.trim()) {
      message.warning("请填写申请理由（策略依据，用于评审留痕）");
      return;
    }
    setCreating(true);
    try {
      const r = await submitApproval({
        type: "exemption",
        agent_id: createAccount,
        payload: { tokens },
        reason: createReason.trim(),
      });
      if (!r.ok) {
        message.warning(r.detail ?? "确定性短路退回（附上次结果/待决满额/冷却中）");
      } else {
        message.success("豁免申请已提交待决（24h 内处理）");
      }
      setCreateOpen(false);
      setCreateTokens("");
      setCreateReason("");
      await reload();
    } catch (e) {
      message.error((e as Error).message ?? "提交失败");
    } finally {
      setCreating(false);
    }
  };

  const strategyAccounts = useMemo(
    () => accounts.filter((a) => a.agent_role === "strategy"),
    [accounts],
  );

  return (
    <div style={{ padding: 16, minHeight: "100%" }}>
      <Flex justify="space-between" align="flex-start" gap={12} wrap>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            审批中心
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            人工评审（决定方=登录用户，spec-04 §4）；确定性短路由 core 判定：相同申请只回放上次结果、
            驳回同类 24h 冷却（可豁免）、每账户每类型待决 ≤3；24h 未决自动过期
          </Typography.Text>
        </div>
        <Flex gap={8} align="center">
          <Button icon={<PlusOutlined />} type="primary" onClick={() => setCreateOpen(true)}>
            新建豁免申请
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => void reload()}>
            刷新
          </Button>
        </Flex>
      </Flex>

      <Card size="small" style={{ marginTop: 12 }} title="审批清单">
        <Flex gap={8} style={{ marginBottom: 12 }}>
          <Segmented
            value={statusFilter}
            onChange={(v) => setStatusFilter(String(v))}
            options={[
              { label: "全部", value: "all" },
              { label: "待决", value: "pending" },
              { label: "已通过", value: "approved" },
              { label: "已驳回", value: "rejected" },
              { label: "已过期", value: "expired" },
            ]}
          />
        </Flex>
        <Table<ApprovalInfo>
          rowKey={(r) => r.id}
          size="small"
          columns={columns}
          dataSource={rows}
          loading={loading}
          pagination={{ pageSize: 15, showSizeChanger: false, showTotal: (t) => `共 ${t} 张` }}
          locale={{
            emptyText: (
              <Empty
                description="尚无审批单——策略侧申请（豁免/风险/粒度…）与日报申报入口将在此汇聚"
                style={{ padding: "24px 0" }}
              />
            ),
          }}
          expandable={{
            expandedRowRender: (r) => (
              <Flex vertical gap={4}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  payload（规范化 JSON）：
                </Typography.Text>
                <pre style={{ margin: 0, fontSize: 12, background: token.colorFillTertiary, padding: 8, borderRadius: 6 }}>
                  {JSON.stringify(r.payload ?? {}, null, 2)}
                </pre>
              </Flex>
            ),
          }}
          scroll={{ x: 900 }}
        />
      </Card>

      <Modal
        title="新建豁免申请（ST/新股买入豁免，spec-04 §4）"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => void createExemption()}
        confirmLoading={creating}
        okText="提交申请"
        cancelText="取消"
      >
        <Flex vertical gap={12} style={{ marginTop: 8 }}>
          <div>
            <Typography.Text type="secondary">目标策略账户</Typography.Text>
            <Select
              style={{ width: "100%", marginTop: 4 }}
              placeholder="选择账户"
              value={createAccount}
              onChange={setCreateAccount}
              options={strategyAccounts.map((a) => ({ value: a.agent_id, label: a.agent_name }))}
            />
          </div>
          <div>
            <Typography.Text type="secondary">豁免 token（如 st 表示允许买入 ST 股，逗号分隔）</Typography.Text>
            <Input
              style={{ marginTop: 4 }}
              placeholder="st, new"
              value={createTokens}
              onChange={(e) => setCreateTokens(e.target.value)}
            />
          </div>
          <div>
            <Typography.Text type="secondary">申请理由（策略依据，评审留痕必填）</Typography.Text>
            <Input.TextArea
              style={{ marginTop: 4 }}
              rows={3}
              value={createReason}
              onChange={(e) => setCreateReason(e.target.value)}
              placeholder="如：拟布局 ST 摘帽行情，风控假设为仓位上限约束下试仓"
            />
          </div>
        </Flex>
      </Modal>

      <Modal
        title="驳回审批单"
        open={rejectTarget !== null}
        onCancel={() => {
          setRejectTarget(null);
          setRejectReason("");
        }}
        onOk={() => {
          if (!rejectTarget) return;
          void doDecide(rejectTarget, "rejected", rejectReason.trim());
          setRejectReason("");
          setRejectTarget(null);
        }}
        okText="确认驳回"
        okButtonProps={{ danger: true }}
        cancelText="取消"
      >
        <Typography.Text type="secondary">请填写驳回理由（可申诉依据）：</Typography.Text>
        <Input.TextArea
          style={{ marginTop: 8 }}
          rows={3}
          placeholder="如：与当前风控大纲冲突，需调整后重新申请"
          value={rejectReason}
          onChange={(e) => setRejectReason(e.target.value)}
        />
      </Modal>
    </div>
  );
}

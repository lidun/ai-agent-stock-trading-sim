import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Col,
  Dropdown,
  Empty,
  Flex,
  Input,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  CaretRightOutlined,
  ClearOutlined,
  ControlOutlined,
  LockOutlined,
  PauseCircleOutlined,
  StopOutlined,
} from "@ant-design/icons";
import {
  batchControl,
  batchSellAll,
  controlAgent,
  emergencySellAll,
  freezeSecurity,
  listAccounts,
  listAgents,
  listFrozen,
  unfreezeSecurity,
  type AccountInfo,
  type AgentInfo,
  type ControlOp,
  type FrozenSecurity,
} from "../../api/endpoints";
import { fmtBeijingTime } from "../../utils/time";

const STATUS_META: Record<string, { color: string; text: string }> = {
  trial: { color: "blue", text: "试运行" },
  running: { color: "green", text: "运行中" },
  paused: { color: "orange", text: "手动暂停" },
  halted: { color: "red", text: "熔断暂停" },
  archived: { color: "default", text: "已归档" },
};

/** 直控台（spec-06 §6.3 全局/单 Agent 聚合视图）：状态总览 + 批量操作 + 冻结清单管理。 */
export default function ControlCenterPage() {
  const { message, modal } = AntApp.useApp();

  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [accounts, setAccounts] = useState<AccountInfo[]>([]);
  const [frozen, setFrozen] = useState<FrozenSecurity[]>([]);
  const [loading, setLoading] = useState(true);

  const [clearAgent, setClearAgent] = useState<AgentInfo | null>(null);
  const [clearConfirm, setClearConfirm] = useState("");
  const [clearing, setClearing] = useState(false);
  const [batchClearOpen, setBatchClearOpen] = useState(false);
  const [batchConfirm, setBatchConfirm] = useState("");
  const [batchClearing, setBatchClearing] = useState(false);

  const [freezeAgent, setFreezeAgent] = useState<string | undefined>();
  const [freezeSymbol, setFreezeSymbol] = useState("");
  const [freezeReason, setFreezeReason] = useState("");
  const [freezing, setFreezing] = useState(false);

  const reload = useCallback(async () => {
    try {
      const [a, ac, f] = await Promise.all([listAgents(), listAccounts(), listFrozen()]);
      setAgents(a.agents);
      setAccounts(ac.accounts);
      setFrozen(f.frozen);
    } catch (e) {
      message.error((e as Error).message ?? "加载直控台失败");
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const mainByAgent = useMemo(() => {
    const m = new Map<string, AccountInfo>();
    accounts.filter((x) => x.role === "main").forEach((x) => m.set(x.agent_id, x));
    return m;
  }, [accounts]);

  const strategies = useMemo(
    () => agents.filter((a) => a.role === "strategy"),
    [agents],
  );
  const running = useMemo(() => strategies.filter((a) => a.status === "running"), [strategies]);
  const frozenAcct = useMemo(
    () =>
      running.filter((a) => {
        const s = mainByAgent.get(a.id)?.status;
        return s === "paused_buy" || s === "halted";
      }),
    [running, mainByAgent],
  );

  const doControl = (agent: AgentInfo, op: ControlOp) => {
    const meta = {
      pause_buy: { title: `冻结买入 · ${agent.name}`, text: "买入即时冻结（保留卖出与风控）" },
      halt: { title: `熔断冻结 · ${agent.name}`, text: "买卖全停（保留结算与风控）" },
      resume: { title: `解除冻结 · ${agent.name}`, text: "主账户恢复 normal，可正常下单" },
    }[op];
    modal.confirm({
      title: meta.title,
      content: <Typography.Text type="secondary">{meta.text}</Typography.Text>,
      okText: op === "resume" ? "恢复" : "确认",
      okButtonProps: { type: op === "resume" ? "primary" : "default", danger: op !== "resume" },
      onOk: async () => {
        try {
          const r = await controlAgent(agent.id, op);
          message.success(`已生效：${r.from} → ${r.to}`);
          void reload();
        } catch (e) {
          message.error((e as Error).message ?? "直控失败");
        }
      },
    });
  };

  const doClearOne = async () => {
    if (!clearAgent || clearConfirm.trim() !== "清仓") return;
    setClearing(true);
    try {
      const r = await emergencySellAll(clearAgent.id);
      if (r.blocked_halted) message.warning("熔断冻结态，卖出被闸门拦截");
      else message.success(r.holdings === 0 ? "无持仓无需清仓" : `已生成 ${r.orders.length} 张卖出单`);
      setClearAgent(null);
      setClearConfirm("");
      void reload();
    } catch (e) {
      message.error((e as Error).message ?? "清仓失败");
    } finally {
      setClearing(false);
    }
  };

  const doBatch = (op: ControlOp) => {
    const meta = {
      pause_buy: { title: "全局冻结买入", text: "作用于全部运行中策略 Agent（非运行/已是目标态自动跳过）" },
      halt: { title: "全局熔断冻结", text: "对全部运行中策略 Agent 买卖全停" },
      resume: { title: "全局解除冻结", text: "恢复全部冻结态主账户为 normal" },
    }[op];
    modal.confirm({
      title: meta.title,
      content: <Typography.Text type="secondary">{meta.text}</Typography.Text>,
      okText: "确认",
      okButtonProps: { type: op === "resume" ? "primary" : "default", danger: op !== "resume" },
      onOk: async () => {
        try {
          const r = await batchControl(op);
          message.success(`生效 ${r.applied_count} 个，跳过 ${r.skipped_count} 个`);
          void reload();
        } catch (e) {
          message.error((e as Error).message ?? "批量直控失败");
        }
      },
    });
  };

  const doBatchClear = async () => {
    if (batchConfirm.trim() !== "清仓") return;
    setBatchClearing(true);
    try {
      const r = await batchSellAll();
      message.success(
        r.total_holdings === 0
          ? "全部运行中策略 Agent 均无持仓"
          : `已生成 ${r.total_orders} 张卖出单（持仓 ${r.total_holdings} 只）`,
      );
      setBatchClearOpen(false);
      setBatchConfirm("");
      void reload();
    } catch (e) {
      message.error((e as Error).message ?? "全局清仓失败");
    } finally {
      setBatchClearing(false);
    }
  };

  const doFreeze = async () => {
    if (!freezeAgent || !freezeSymbol.trim()) {
      message.warning("请选择目标 Agent 并填写证券代码");
      return;
    }
    setFreezing(true);
    try {
      const r = await freezeSecurity(freezeAgent, freezeSymbol.trim(), freezeReason.trim());
      message.success(`已冻结 ${r.symbol}（取消买入单 ${r.cancelled_buy_orders} 张）`);
      setFreezeSymbol("");
      setFreezeReason("");
      void reload();
    } catch (e) {
      message.error((e as Error).message ?? "冻结失败");
    } finally {
      setFreezing(false);
    }
  };

  const doUnfreeze = (row: FrozenSecurity) => {
    modal.confirm({
      title: `解除冻结 ${row.symbol}（${row.agent_id}）？`,
      content: "解除后恢复该 Agent 买入此证券。",
      okText: "解除冻结",
      okButtonProps: { type: "primary" },
      onOk: async () => {
        try {
          await unfreezeSecurity(row.agent_id, row.symbol);
          message.success(`已解除 ${row.symbol}`);
          void reload();
        } catch (e) {
          message.error((e as Error).message ?? "解除失败");
        }
      },
    });
  };

  const runningNames = useMemo(() => {
    const m = new Map<string, string>();
    running.forEach((a) => m.set(a.id, a.name));
    return m;
  }, [running]);

  const batchMenu = [
    { key: "pause_buy", icon: <PauseCircleOutlined />, label: "全部冻结买入（保留卖出）" },
    { key: "halt", icon: <StopOutlined />, label: "全部熔断冻结（买卖全停）" },
    { key: "resume", icon: <CaretRightOutlined />, label: "全部解除冻结 / 恢复" },
    { type: "divider" as const },
    { key: "clear", icon: <ClearOutlined />, danger: true, label: "全部紧急清仓" },
  ];

  return (
    <div style={{ padding: 16, minHeight: "100%" }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 12 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            直控台
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            强指令下发与手工干预通道（spec-01 §5.3 / spec-06 §6.3）——全局与单 Agent 两层即时生效
          </Typography.Text>
        </div>
        <Dropdown
          trigger={["click"]}
          menu={{
            items: batchMenu,
            onClick: ({ key }) =>
              key === "clear"
                ? (setBatchConfirm(""), setBatchClearOpen(true))
                : doBatch(key as ControlOp),
          }}
        >
          <Button type="primary" icon={<ControlOutlined />}>
            全局直控
          </Button>
        </Dropdown>
      </Flex>

      {running.length === 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="当前无运行中的策略 Agent，直控操作仅可作用于运行中账户。"
        />
      )}

      <Row gutter={12} style={{ marginBottom: 12 }}>
        <Col span={8}>
          <Card size="small">
            <Statistic title="运行中策略 Agent" value={running.length} suffix={`/ ${strategies.length}`} />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small">
            <Statistic
              title="处于冻结态的账户"
              value={frozenAcct.length}
              valueStyle={{ color: frozenAcct.length ? "#fa8c16" : undefined }}
              suffix={`/ ${running.length}`}
            />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small">
            <Statistic title="冻结证券条目" value={frozen.length} prefix={<LockOutlined />} />
          </Card>
        </Col>
      </Row>

      <Card size="small" title="运行中策略 Agent 状态" style={{ marginBottom: 12 }}>
        <Table<AgentInfo>
          rowKey="id"
          size="small"
          loading={loading}
          dataSource={running}
          pagination={false}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无运行中策略 Agent" /> }}
          columns={[
            { title: "Agent", key: "name", render: (_, a) => <Typography.Text strong>{a.name}</Typography.Text> },
            {
              title: "状态", key: "status", width: 110,
              render: (_, a) => {
                const m = STATUS_META[a.status] ?? { color: "default", text: a.status };
                return <Tag color={m.color}>{m.text}</Tag>;
              },
            },
            {
              title: "主账户", key: "acct", width: 160,
              render: (_, a) => {
                const s = mainByAgent.get(a.id)?.status ?? "-";
                const meta: Record<string, { color: string; text: string }> = {
                  normal: { color: "green", text: "正常" },
                  paused_buy: { color: "orange", text: "冻结买入（保留卖出）" },
                  halted: { color: "red", text: "熔断冻结" },
                };
                const m = meta[s];
                return m ? <Tag color={m.color}>{m.text}</Tag> : <span>{s}</span>;
              },
            },
            {
              title: "冻结证券", key: "fz", width: 200,
              render: (_, a) => {
                const list = frozen.filter((f) => f.agent_id === a.id).map((f) => f.symbol);
                return list.length ? (
                  <Space size={4} wrap>
                    {list.map((s) => (
                      <Tag key={s} color="orange">{s}</Tag>
                    ))}
                  </Space>
                ) : (
                  <Typography.Text type="secondary">—</Typography.Text>
                );
              },
            },
            {
              title: "操作", key: "op", width: 300,
              render: (_, a) => {
                const acct = mainByAgent.get(a.id)?.status;
                const frozenState = acct === "paused_buy" || acct === "halted";
                return (
                  <Space size={4} wrap>
                    {!frozenState ? (
                      <>
                        <Button size="small" icon={<PauseCircleOutlined />} onClick={() => doControl(a, "pause_buy")}>
                          冻结买入
                        </Button>
                        <Button size="small" danger icon={<StopOutlined />} onClick={() => doControl(a, "halt")}>
                          熔断
                        </Button>
                      </>
                    ) : (
                      <Button size="small" type="primary" ghost icon={<CaretRightOutlined />} onClick={() => doControl(a, "resume")}>
                        解除冻结
                      </Button>
                    )}
                    <Tooltip title="清仓需输入确认">
                      <Button
                        size="small"
                        danger
                        ghost
                        icon={<ClearOutlined />}
                        onClick={() => {
                          setClearConfirm("");
                          setClearAgent(a);
                        }}
                      >
                        清仓
                      </Button>
                    </Tooltip>
                  </Space>
                );
              },
            },
          ]}
        />
      </Card>

      <Card size="small" title="冻结证券管理（新增 / 解除）">
        <Space.Compact style={{ display: "flex", marginBottom: 12 }}>
          <Select
            placeholder="目标 Agent（运行中）"
            style={{ minWidth: 220 }}
            value={freezeAgent}
            onChange={(v) => setFreezeAgent(v)}
            options={running.map((a) => ({ value: a.id, label: a.name }))}
            showSearch
            optionFilterProp="label"
          />
          <Input
            placeholder="证券代码，如 600519"
            value={freezeSymbol}
            onChange={(e) => setFreezeSymbol(e.target.value)}
            maxLength={16}
          />
          <Input
            placeholder="原因（可选）"
            value={freezeReason}
            onChange={(e) => setFreezeReason(e.target.value)}
            maxLength={200}
          />
          <Button type="primary" icon={<LockOutlined />} loading={freezing} onClick={() => void doFreeze()}>
            冻结
          </Button>
        </Space.Compact>
        <Table<FrozenSecurity>
          rowKey="id"
          size="small"
          loading={loading}
          dataSource={frozen}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前无冻结证券" /> }}
          columns={[
            {
              title: "Agent", dataIndex: "agent_id", width: 260,
              render: (id: string) => runningNames.get(id) ?? id,
            },
            { title: "证券", dataIndex: "symbol", width: 120 },
            { title: "原因", dataIndex: "reason", ellipsis: true },
            { title: "冻结时间", dataIndex: "created_ts", width: 170, render: (v: string) => fmtBeijingTime(v) },
            {
              title: "操作", key: "op", width: 100,
              render: (_, row) => (
                <Button size="small" onClick={() => doUnfreeze(row)}>
                  解除
                </Button>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title={`紧急清仓 · ${clearAgent?.name ?? ""}`}
        open={clearAgent !== null}
        onCancel={() => {
          setClearAgent(null);
          setClearConfirm("");
        }}
        onOk={() => void doClearOne()}
        confirmLoading={clearing}
        okText="确认清仓"
        okButtonProps={{ danger: true, disabled: clearConfirm.trim() !== "清仓" }}
      >
        <Input
          placeholder="请输入“清仓”以确认本次高危操作"
          value={clearConfirm}
          onChange={(e) => setClearConfirm(e.target.value)}
          maxLength={16}
        />
      </Modal>

      <Modal
        title="全局紧急清仓（需输入确认）"
        open={batchClearOpen}
        onCancel={() => {
          setBatchClearOpen(false);
          setBatchConfirm("");
        }}
        onOk={() => void doBatchClear()}
        confirmLoading={batchClearing}
        okText="确认全局清仓"
        okButtonProps={{ danger: true, disabled: batchConfirm.trim() !== "清仓" }}
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
          将为全部运行中策略 Agent 的主账户逐票生成市价卖出条件单；熔断冻结中的 Agent 会被闸门拦截。
        </Typography.Paragraph>
        <Input
          placeholder="请输入“清仓”以确认本次高危操作"
          value={batchConfirm}
          onChange={(e) => setBatchConfirm(e.target.value)}
          maxLength={16}
        />
      </Modal>
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  App as AntApp,
  Button,
  Descriptions,
  Drawer,
  Flex,
  Input,
  List,
  Skeleton,
  Space,
  Statistic,
  Tag,
  Typography,
} from "antd";
import {
  CheckCircleFilled,
  CloseCircleFilled,
  ExperimentOutlined,
  MinusCircleOutlined,
  RiseOutlined,
} from "@ant-design/icons";
import {
  finishTrial,
  trialProgress,
  type AgentInfo,
  type TrialProgress,
} from "../../api/endpoints";
import { fmtBeijingTime } from "../../utils/time";

/** 试运行验收看板（spec-05 §6.1/§6.2 + spec-06 §6.4）：门槛预览 → launch/reject 决策留证。 */
export default function TrialAcceptance({
  agent,
  open,
  onClose,
  onDone,
}: {
  agent: AgentInfo;
  open: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const { message, modal } = AntApp.useApp();
  const [progress, setProgress] = useState<TrialProgress | null>(null);
  const [loading, setLoading] = useState(false);
  const [verdict, setVerdict] = useState("");
  const [submitting, setSubmitting] = useState<"launch" | "reject" | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setProgress(await trialProgress(agent.id));
    } catch (e) {
      message.error((e as Error).message ?? "加载验收进度失败");
    } finally {
      setLoading(false);
    }
  }, [agent.id, message]);

  useEffect(() => {
    if (open) {
      setVerdict("");
      void reload();
    }
  }, [open, reload]);

  const decide = (decision: "launch" | "reject") => {
    const isLaunch = decision === "launch";
    modal.confirm({
      title: isLaunch ? "通过试运行并上线？" : "否决并归档该策略？",
      icon: <ExperimentOutlined />,
      okText: isLaunch ? "通过 · 上线" : "确认否决",
      okButtonProps: { type: isLaunch ? "primary" : "default", danger: !isLaunch },
      cancelText: "再想想",
      content: isLaunch ? (
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          上线后该 trial 账户将整体归档为不可变验收证据（订单/成交/结算计数快照一次写入），
          主账户（10 万种子资金）零污染接续正常交易（spec-01 §2.8）。
        </Typography.Text>
      ) : (
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          否决后 Agent 与 trial 账户均转为归档态（archived），停止参与结算；回放证据随验收留档。
        </Typography.Text>
      ),
      onOk: async () => {
        setSubmitting(decision);
        try {
          const r = await finishTrial(agent.id, decision, verdict.trim());
          message.success(
            decision === "launch" ? "已上线，验收证据已归档留证" : "已否决并归档",
          );
          modal.info({
            title: "验收归档留证完成",
            content: (
              <Flex vertical gap={4}>
                <Typography.Text>
                  归档编号：{r.archive_id} · 决策：{decision === "launch" ? "上线" : "否决"}
                </Typography.Text>
                {verdict.trim() && (
                  <Typography.Text type="secondary">验收意见：{verdict.trim()}</Typography.Text>
                )}
              </Flex>
            ),
          });
          onDone();
        } catch (e) {
          message.error((e as Error).message ?? "验收决策失败");
        } finally {
          setSubmitting(null);
        }
      },
    });
  };

  const gatesReady = progress?.gates?.window_ok && progress?.gates?.attempt_ok && progress?.gates?.abnormal_ok;

  return (
    <Drawer
      title={
        <Space>
          <ExperimentOutlined />
          <span>试运行验收 · {agent.name}</span>
          <Tag color="blue">试运行</Tag>
        </Space>
      }
      width={560}
      open={open}
      onClose={onClose}
      destroyOnClose
      extra={
        <Space>
          <Button
            icon={<MinusCircleOutlined />}
            loading={submitting === "reject"}
            onClick={() => decide("reject")}
          >
            否决归档
          </Button>
          <Button
            type="primary"
            icon={<RiseOutlined />}
            disabled={!gatesReady}
            loading={submitting === "launch"}
            onClick={() => decide("launch")}
            title={progress?.reasons?.join("；")}
          >
            通过 · 上线
          </Button>
        </Space>
      }
    >
      {loading || !progress ? (
        <Skeleton active paragraph={{ rows: 8 }} />
      ) : (
        <Flex vertical gap={16}>
          <Descriptions
            size="small"
            column={3}
            bordered
            items={[
              { key: "win", label: "回放窗口", children: `${progress.sessions_done} / ${progress.window_days} 天` },
              { key: "settle", label: "已结算日", children: progress.settle_days },
              { key: "orders", label: "条件单尝试", children: progress.condition_orders },
            ]}
          />
          <Space size={8} wrap>
            <Statistic title="回放进度" value={progress.sessions_done} suffix={`/ ${progress.window_days}`} valueStyle={{ fontSize: 20 }} />
            <Tag icon={progress.gates.window_ok ? <CheckCircleFilled /> : <CloseCircleFilled />} color={progress.gates.window_ok ? "success" : "error"}>
              窗口满 {progress.gates.window_ok ? "已达成" : "未达成"}
            </Tag>
            <Tag icon={progress.gates.attempt_ok ? <CheckCircleFilled /> : <CloseCircleFilled />} color={progress.gates.attempt_ok ? "success" : "error"}>
              条件单尝试 {progress.gates.attempt_ok ? "已有" : "尚无"}
            </Tag>
            <Tag icon={progress.gates.abnormal_ok ? <CheckCircleFilled /> : <CloseCircleFilled />} color={progress.gates.abnormal_ok ? "success" : "error"}>
              结算异常 {progress.gates.abnormal_ok ? "无" : `${progress.abnormal_dates.length} 日`}
            </Tag>
          </Space>

          {progress.reasons.length > 0 && (
            <Alert type="error" showIcon message="尚未满足上线硬门槛（spec-05 §6.1）" description={<List size="small" dataSource={progress.reasons} renderItem={(r) => <List.Item>{r}</List.Item>} />} />
          )}

          {progress.sessions.length > 0 && (
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                已回放交易日：{progress.sessions.map((d) => fmtBeijingTime(d).slice(0, 10)).join("、")}
              </Typography.Text>
            </div>
          )}

          {progress.abnormal_dates.length > 0 && (
            <Alert type="warning" showIcon message="规则级异常日（有订单但当日结算未落账）" description={progress.abnormal_dates.join("、")} />
          )}

          <Input.TextArea
            rows={3}
            maxLength={500}
            placeholder="验收意见（留证，如：回放窗口满、超额收益达标、回撤可控；否决请注明原因）"
            value={verdict}
            onChange={(e) => setVerdict(e.target.value)}
            showCount
          />
        </Flex>
      )}
    </Drawer>
  );
}

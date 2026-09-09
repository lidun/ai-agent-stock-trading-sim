import { Card, Empty, Skeleton, Tag, Typography } from "antd";
import type { ValidationWindowList } from "../../api/endpoints";
import ValidationWindowsTable from "./ValidationWindowsTable";

interface Props {
  windows: ValidationWindowList | null;
  loading: boolean;
}

export default function ValidationWindowsCard({ windows, loading }: Props) {
  const running = (windows?.items ?? []).filter((w) => w.status === "in_progress").length;
  return (
    <Card
      size="small"
      title="EVOQUANT 验证窗台账 · 引擎判定留证（spec-05 §4.2/§4.3）"
      extra={
        windows && (
          <Tag color={running ? "blue" : "default"} style={{ marginInlineEnd: 0 }}>
            {running ? `在跑 ${running} 窗` : "无在跑窗"}
          </Tag>
        )
      }
    >
      {loading ? (
        <Skeleton active paragraph={{ rows: 4 }} />
      ) : !windows || windows.total === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="尚无验证窗——EVOQUANT 对候选版本发起验证后，每会话日推进、到期判定（activate / rollback / sealed）在此留证"
          style={{ padding: "12px 0" }}
        />
      ) : (
        <>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
            独立验证账户跑真实撮合：期望值取验证窗内卖出成交净盈亏（含双边费）的均值 %，
            与主账户同窗同期基线对比判定。窗口收口即归档验证账户，候选版本否决/晋升由引擎落账，页面只读。
          </Typography.Paragraph>
          <ValidationWindowsTable dataSource={windows.items} />
        </>
      )}
    </Card>
  );
}

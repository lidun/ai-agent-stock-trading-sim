import { useEffect, useMemo, useRef, useState } from "react";
import { Card, Checkbox, Empty, Flex, Segmented, Skeleton, Tag, Typography } from "antd";
import { theme } from "antd";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { ECharts } from "echarts/core";
import type { ComposeOption } from "echarts/core";
import type { GridComponentOption, LegendComponentOption, TooltipComponentOption } from "echarts/components";
import type { LineSeriesOption } from "echarts/charts";
import type { CurveRange, EquityCurve } from "../../api/endpoints";
import { CHART_COLORS } from "../../styles/tokens";

echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

type ECOption = ComposeOption<
  LineSeriesOption | GridComponentOption | LegendComponentOption | TooltipComponentOption
>;

const RANGE_OPTS: { value: CurveRange; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "3m", label: "近三月" },
  { value: "1m", label: "近一月" },
];

interface Props {
  agentName: string;
  curve: EquityCurve | null;
  loading: boolean;
  range: CurveRange;
  onRangeChange: (r: CurveRange) => void;
}

export default function EquityCurveCard({ agentName, curve, loading, range, onRangeChange }: Props) {
  const { token } = theme.useToken();
  const domRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<ECharts | null>(null);
  const roRef = useRef<ResizeObserver | null>(null);
  const lastNodeRef = useRef<HTMLDivElement | null>(null);
  const [overlay, setOverlay] = useState<string[]>([]);

  const main = curve?.series.find((s) => s.role === "main") ?? null;
  const overlays = useMemo(
    () => (curve?.series ?? []).filter((s) => s.role !== "main" && s.points.length > 0),
    [curve],
  );
  const bench = curve?.benchmark;

  const selectedOverlays = overlays.filter((s) => overlay.includes(s.account_id));

  const ensureChart = (): ECharts | null => {
    if (!domRef.current) return null;
    chartRef.current ??= echarts.init(domRef.current);
    return chartRef.current;
  };

  const visible = useMemo(() => {
    if (!curve || !main || main.points.length === 0) return null;
    return [
      { account_id: main.account_id, label: main.label, points: main.points },
      ...selectedOverlays.map((s) => ({ account_id: s.account_id, label: s.label, points: s.points })),
    ];
  }, [curve, main, selectedOverlays]);

  useEffect(() => {
    const el = domRef.current;
    if (el !== lastNodeRef.current) {
      if (lastNodeRef.current) {
        chartRef.current?.dispose();
        chartRef.current = null;
        roRef.current?.disconnect();
        roRef.current = null;
      }
      lastNodeRef.current = el;
    }
  });

  useEffect(() => {
    if (loading) return;
    const chart = ensureChart();
    const el = domRef.current;
    if (!chart || !el || !curve || !main || !visible || main.points.length === 0) return;
    if (!roRef.current) {
      roRef.current = new ResizeObserver(() => chart.resize());
      roRef.current.observe(el);
    }

    const dateKey = new Set<string>();
    visible.forEach((s) => s.points.forEach((p) => dateKey.add(p.trade_date)));
    if (bench?.available) bench.points.forEach((p) => dateKey.add(p.trade_date));
    const dates = Array.from(dateKey).sort();
    if (dates.length === 0) return;

    const toMap = (pts: Array<{ trade_date: string; return_pct: number }>) => {
      const map = new Map<string, number>();
      pts.forEach((p) => map.set(p.trade_date, p.return_pct));
      return map;
    };
    const axisValue = (m: Map<string, number>) => dates.map((d) => m.get(d) ?? null);

    const series: ECOption["series"] = visible.map((s, i) => ({
      name: s.label,
      type: "line",
      data: axisValue(toMap(s.points)),
      connectNulls: true,
      symbol: "none",
      smooth: true,
      lineStyle: { width: 2, color: CHART_COLORS[i % CHART_COLORS.length] },
      itemStyle: { color: CHART_COLORS[i % CHART_COLORS.length] },
    }));
    if (bench?.available) {
      series.push({
        name: "沪深300",
        type: "line",
        data: axisValue(toMap(bench.points)),
        connectNulls: true,
        symbol: "none",
        smooth: true,
        lineStyle: { width: 1.5, type: "dashed", color: "#8c8c8c" },
        itemStyle: { color: "#8c8c8c" },
      });
    }

    chart.setOption(
      {
        tooltip: {
          trigger: "axis",
          valueFormatter: (v: unknown) => (typeof v === "number" ? `${v.toFixed(2)}%` : "—"),
        },
        legend: { top: 0, type: "scroll", textStyle: { color: token.colorTextSecondary, fontSize: 12 } },
        grid: { top: 34, left: 8, right: 16, bottom: 4, containLabel: true },
        xAxis: {
          type: "category",
          boundaryGap: false,
          data: dates,
          axisLabel: { color: token.colorTextSecondary, fontSize: 11, hideOverlap: true },
          axisLine: { lineStyle: { color: token.colorBorderSecondary } },
          axisTick: { show: false },
        },
        yAxis: {
          type: "value",
          scale: true,
          axisLabel: { color: token.colorTextSecondary, fontSize: 11, formatter: "{value}%" },
          splitLine: { lineStyle: { color: token.colorSplit, type: "dashed" } },
        },
        series,
      },
      { notMerge: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, curve, main, visible, bench, token]);

  useEffect(() => {
    return () => {
      roRef.current?.disconnect();
      roRef.current = null;
      chartRef.current?.dispose();
      chartRef.current = null;
    };
  }, []);

  const empty =
    !loading && (!curve || !main || main.points.length === 0) ? (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="暂无净值样本——EOD 结算产出后自动绘制（spec-04 §5 结算 → 日报）"
        style={{ padding: "16px 0" }}
      />
    ) : null;

  return (
    <Card
      size="small"
      style={{ marginBottom: 12 }}
      title={`资金曲线 · ${agentName}`}
      extra={
        <Flex gap={12} align="center">
          {!empty && (
            <Segmented
              size="small"
              options={RANGE_OPTS}
              value={range}
              onChange={(v) => onRangeChange(v as CurveRange)}
            />
          )}
        </Flex>
      }
    >
      {loading ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : empty ? (
        empty
      ) : (
        <>
          {overlays.length > 0 && (
            <Flex gap={4} wrap align="center" style={{ marginBottom: 8 }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                叠加角色曲线：
              </Typography.Text>
              <Checkbox.Group
                options={overlays.map((s, i) => ({
                  label: (
                    <Tag color={CHART_COLORS[(i + 1) % CHART_COLORS.length]} style={{ marginInlineEnd: 0 }}>
                      {s.label}
                    </Tag>
                  ),
                  value: s.account_id,
                }))}
                value={overlay}
                onChange={(v) => setOverlay(v as string[])}
              />
            </Flex>
          )}
          <div ref={domRef} style={{ width: "100%", height: 340 }} />
          <Flex wrap gap={16} style={{ marginTop: 8 }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              纵轴为区间累计收益率（%）——以各账户首个净值日为锚；基准为沪深300 当日收盘对齐（sh000300）。
            </Typography.Text>
            {bench && !bench.available && (
              <Tag color="orange">基准不可用：{bench.reason || "行情源缺口"}（曲线不受阻）</Tag>
            )}
          </Flex>
        </>
      )}
    </Card>
  );
}

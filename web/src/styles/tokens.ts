/** 全局涨跌语义色（A 股：红涨绿跌，spec-06 §5.1，与 global.css --up/--down 同源）。 */
export const UP_COLOR = "#cf1322";
export const DOWN_COLOR = "#389e0d";
export const FLAT_COLOR = "#8c8c8c";

/** 曲线/序列配色（资金曲线主色取 antd primary 之外的非语义色，避免与涨跌语义混用）。 */
export const CHART_COLORS = ["#1677ff", "#fa8c16", "#722ed1", "#13c2c2", "#eb2f96"];

export function colorOfSign(v: number | string | null | undefined): string {
  const n = Number(v ?? "0");
  if (n > 0) return UP_COLOR;
  if (n < 0) return DOWN_COLOR;
  return FLAT_COLOR;
}

export function pctText(v: number | null | undefined, signed = true): string {
  if (v == null || Number.isNaN(v)) return "—";
  const n = Number(v);
  return `${signed && n > 0 ? "+" : ""}${n.toFixed(2)}%`;
}

export function moneyText(v: number | string | null | undefined, digits = 2): string {
  if (v == null || v === "") return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return "—";
  return n.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

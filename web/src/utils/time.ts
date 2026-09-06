import dayjs from "dayjs";

/** 北京时间常量（UTC+8，固定口径，spec-06 §5.2 京角标） */
const BJ_OFFSET_MS = 8 * 3600 * 1000;

function bj(iso: string): dayjs.Dayjs {
  return dayjs(Date.parse(iso) + BJ_OFFSET_MS);
}

/** ISO(UTC) → 北京时区 'YYYY-MM-DD HH:mm' */
export function fmtBeijing(iso: string): string {
  return bj(iso).format("YYYY-MM-DD HH:mm");
}

/** ISO(UTC) → 北京时区 'HH:mm' */
export function fmtBeijingTime(iso: string): string {
  return bj(iso).format("HH:mm");
}

/** ISO(UTC) → 北京时区日期分隔线文案 */
export function daySeparator(iso: string): string {
  const d = bj(iso);
  const today = dayjs(Date.now() + BJ_OFFSET_MS).startOf("day");
  const diff = today.diff(d.startOf("day"), "day");
  if (diff === 0) return "今天";
  if (diff === 1) return "昨天";
  return d.format("YYYY-MM-DD");
}

export function isSameBeijingDay(a: string, b: string): boolean {
  return bj(a).format("YYYY-MM-DD") === bj(b).format("YYYY-MM-DD");
}

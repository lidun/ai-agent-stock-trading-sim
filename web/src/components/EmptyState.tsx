import type { ReactNode } from "react";

/** 空态/加载/错误三态规范（spec-06 §5.4） */
export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div style={{ padding: "48px 16px", textAlign: "center" }}>
      <div style={{ fontSize: 40, lineHeight: 1, marginBottom: 12, opacity: 0.55 }}>
        {icon ?? "🗂"}
      </div>
      <div style={{ fontWeight: 600, fontSize: 16 }}>{title}</div>
      {description && (
        <div className="muted" style={{ marginTop: 6, maxWidth: 420, marginInline: "auto" }}>
          {description}
        </div>
      )}
      {action && <div style={{ marginTop: 16 }}>{action}</div>}
    </div>
  );
}

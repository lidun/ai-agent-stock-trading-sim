import { Card, Button } from "antd";
import { useNavigate } from "react-router-dom";
import { EmptyState } from "../components/EmptyState";
import type { NavItem } from "../config/nav";

/** 未落地模块的占位页（P1/P2/P3 分阶段填充） */
export function ModulePlaceholder({ nav }: { nav: NavItem }) {
  const navigate = useNavigate();
  return (
    <Card
      bordered
      style={{ marginTop: 16, minHeight: 320 }}
      bodyStyle={{ display: "flex", alignItems: "center", justifyContent: "center" }}
    >
      <EmptyState
        icon={nav.icon}
        title={`${nav.label} · ${nav.eta} 交付`}
        description={nav.desc}
        action={
          nav.eta === "P1" ? (
            <Button type="primary" onClick={() => navigate("/chat")}>
              返回对话面板
            </Button>
          ) : undefined
        }
      />
    </Card>
  );
}

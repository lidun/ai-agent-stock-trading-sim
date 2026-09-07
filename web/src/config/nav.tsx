import type { ReactNode } from "react";
import {
  ApartmentOutlined,
  AppstoreOutlined,
  BellOutlined,
  BookOutlined,
  DashboardOutlined,
  DeploymentUnitOutlined,
  FileTextOutlined,
  FundOutlined,
  LineChartOutlined,
  RobotOutlined,
  SettingOutlined,
  StockOutlined,
  WechatOutlined,
} from "@ant-design/icons";

export interface NavItem {
  path: string;
  key: string;
  label: string;
  icon: ReactNode;
  /** 该模块预期交付阶段（用于占位页提示） */
  eta: string;
  desc: string;
}

/** 一级导航 13 模块（spec-06 §4.1） */
export const NAV_ITEMS: NavItem[] = [
  {
    path: "/chat",
    key: "chat",
    label: "对话面板",
    icon: <WechatOutlined />,
    eta: "P1",
    desc: "单 Agent 多会话对话、消息流式展示、会话树操作与语境管理（spec-01 §4）",
  },
  {
    path: "/agents",
    key: "agents",
    label: "Agent看板",
    icon: <RobotOutlined />,
    eta: "P1",
    desc: "Agent 列表、工作台、账户风控与全局/独立视角切换（spec-01 §5.1）",
  },
  {
    path: "/control",
    key: "control",
    label: "直控台",
    icon: <DeploymentUnitOutlined />,
    eta: "P2",
    desc: "Agent 强指令下发与手工干预通道（spec-01 §5.3）",
  },
  {
    path: "/strategies",
    key: "strategies",
    label: "策略详情",
    icon: <ApartmentOutlined />,
    eta: "P2",
    desc: "策略生命周期：状态流转、生效日历、归因版本（spec-01 §5.2 / spec-05）",
  },
  {
    path: "/trading",
    key: "trading",
    label: "交易中心",
    icon: <StockOutlined />,
    eta: "P2",
    desc: "股票/两融/期权交易执行与持仓管理",
  },
  {
    path: "/reports",
    key: "reports",
    label: "日报中心",
    icon: <FileTextOutlined />,
    eta: "P1",
    desc: "引擎数据段日报时间线与阅读（spec-04 §5.2/§5.3：数据段零 token、缺勤/修订版本切换；spec-06 §6.6）",
  },
  {
    path: "/knowledge",
    key: "knowledge",
    label: "知识库",
    icon: <BookOutlined />,
    eta: "P2",
    desc: "知识与经验库建设（spec-05）",
  },
  {
    path: "/capabilities",
    key: "capabilities",
    label: "能力市场",
    icon: <AppstoreOutlined />,
    eta: "P3",
    desc: "能力注册/审核/配额与组合编排（spec-04）",
  },
  {
    path: "/charter",
    key: "charter",
    label: "大纲管理",
    icon: <DashboardOutlined />,
    eta: "P3",
    desc: "全局原则、投资大纲与审批意见管理（spec-01 §7）",
  },
  {
    path: "/approvals",
    key: "approvals",
    label: "审批中心",
    icon: <BellOutlined />,
    eta: "P2",
    desc: "人工审批流：授权-指令-处置-日报（spec-01 §7）",
  },
  {
    path: "/performance",
    key: "performance",
    label: "性能监控",
    icon: <LineChartOutlined />,
    eta: "P3",
    desc: "Agent 吞吐/延迟/任务画像与系统性能（spec-04 §9）",
  },
  {
    path: "/market",
    key: "market",
    label: "市场与数据监控",
    icon: <FundOutlined />,
    eta: "P2",
    desc: "行情、交易核对与数据质量监控（spec-02）",
  },
  {
    path: "/settings",
    key: "settings",
    label: "设置",
    icon: <SettingOutlined />,
    eta: "P1",
    desc: "账户安全（口令/会话）、连接状态与系统偏好",
  },
];

export const DB_NAV_PATH = "/market";

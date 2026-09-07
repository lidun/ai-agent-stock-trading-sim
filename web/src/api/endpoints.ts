import { apiGet, apiPatch, apiPost } from "./client";

export interface Me {
  user: string;
  login_at: string;
  core_version: string;
}

export interface SessionInfo {
  token_hash: string;
  created_at: string;
  last_seen_at: string;
  ip: string;
  user_agent: string;
  revoked: number;
  expires_at: string;
}

export interface SessionsList {
  current_token_hash: string;
  sessions: SessionInfo[];
}

export function fetchMe(): Promise<Me> {
  return apiGet<Me>("/api/auth/me");
}

export function login(username: string, password: string): Promise<{ ok: true; user: string }> {
  return apiPost("/api/auth/login", { username, password });
}

export function logout(): Promise<{ ok: true }> {
  return apiPost("/api/auth/logout");
}

export function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<{ ok: true }> {
  return apiPost("/api/auth/change_password", {
    current_password: currentPassword,
    new_password: newPassword,
  });
}

export function listSessions(): Promise<SessionsList> {
  return apiGet<SessionsList>("/api/auth/sessions");
}

export function revokeSession(tokenHash: string): Promise<{ ok: true }> {
  return apiPost(`/api/auth/sessions/${encodeURIComponent(tokenHash)}/revoke`);
}

export interface Health {
  ok: boolean;
  service: string;
  version: string;
  env: string;
  uptime_s: number;
  db: boolean;
  single_instance: boolean;
}

export function fetchHealth(): Promise<Health> {
  return apiGet<Health>("/api/health");
}

// ---------- 对话域（spec-06 §6.1 / spec-02 §6.2 契约） ----------

export type AgentRole = "manager" | "strategy";
export type ConvType = "user_chat" | "report_direct" | "sync";
export type MessageStatus = "queued" | "processing" | "delivered" | "pending_review" | "failed";
export type MessageDirection = "user" | "agent";

export interface AgentInfo {
  id: string;
  name: string;
  role: AgentRole;
  status: string;
  created_ts: string;
}

export interface LastMessage {
  id: string;
  direction: MessageDirection;
  msg_type: string;
  status: MessageStatus;
  body: string;
  ts: string;
}

export interface ConversationInfo {
  id: string;
  agent_id: string;
  agent_name: string;
  agent_role: AgentRole;
  agent_status: string;
  conv_type: ConvType;
  created_ts: string;
  unread: number;
  last_message: LastMessage | null;
}

export interface MessageInfo {
  id: string;
  conv_id: string;
  agent_id: string;
  direction: MessageDirection;
  msg_type: string;
  body: string;
  payload: unknown;
  delivered_via: string;
  status: MessageStatus;
  delivery_attempts: number;
  read_ts: string | null;
  ts: string;
}

export function listAgents(): Promise<{ agents: AgentInfo[] }> {
  return apiGet("/api/agents");
}

export function listConversations(): Promise<{ conversations: ConversationInfo[] }> {
  return apiGet("/api/conversations");
}

export function openConversation(agentId: string): Promise<ConversationInfo> {
  return apiPost("/api/conversations", { agent_id: agentId });
}

export function listMessages(
  convId: string,
  before?: { ts: string; id: string },
): Promise<{
  conversation: ConversationInfo;
  messages: MessageInfo[];
  has_older: boolean;
  next_before_ts: string | null;
  next_before_id: string | null;
}> {
  const q = before ? `?before_ts=${encodeURIComponent(before.ts)}&before_id=${encodeURIComponent(before.id)}` : "";
  return apiGet(`/api/conversations/${encodeURIComponent(convId)}/messages${q}`);
}

export function markConversationRead(convId: string): Promise<{ updated: number }> {
  return apiPost(`/api/conversations/${encodeURIComponent(convId)}/read`);
}

export function sendMessage(convId: string, body: string): Promise<{ message: MessageInfo }> {
  return apiPost(`/api/conversations/${encodeURIComponent(convId)}/messages`, { body });
}

// ---------- 账户域（spec-01 §2.1 accounts，金额为精度字符串） ----------

export interface AccountInfo {
  id: string;
  agent_id: string;
  agent_name: string;
  agent_role: string;
  agent_status: string;
  initial_capital: string;
  cash: string;
  nav: string;
  shares: string;
  total_pnl: string;
  today_pnl: string;
  granularity: "eod_replay" | "intraday_5m" | "intraday_1m";
  settle_key: string;
  status: string;
  active_version_no: string;
  created_ts: string;
  updated_ts: string;
}

export function listAccounts(): Promise<{ accounts: AccountInfo[] }> {
  return apiGet("/api/accounts");
}

export function getAccount(agentId: string): Promise<AccountInfo> {
  return apiGet(`/api/accounts/${encodeURIComponent(agentId)}`);
}

// ---------- 交易存储域（spec-01 §2.2/§2.3 只读；引擎写入后呈现，当前空态） ----------

export interface LotInfo {
  id: string;
  buy_trade_id: string;
  buy_date: string;
  buy_price: string;
  quantity: string;
  remaining: string;
  strategy_version_no: string;
}

export interface HoldingInfo {
  id: string;
  account_id: string;
  symbol: string;
  quantity: string;
  avg_cost: string;
  updated_ts: string;
  lots: LotInfo[];
}

export interface ConditionOrderInfo {
  id: string;
  account_id: string;
  order_type: string;
  direction: "buy" | "sell";
  scope: string;
  symbol: string;
  symbols: string[];
  trigger: string;
  basis: string;
  price_ref: string;
  qty: string;
  amount: string;
  budget: string;
  price_type: string;
  limit_price: string;
  validity: string;
  valid_until: string;
  priority: number;
  status: string;
  insufficient_events: number;
  invalid_reason: string;
  strategy_version_no: string;
  created_at: string;
  creator: string;
  reason: string;
  settled_on: string;
}

export function listAccountHoldings(
  agentId: string,
): Promise<{ account_id: string; holdings: HoldingInfo[] }> {
  return apiGet(`/api/accounts/${encodeURIComponent(agentId)}/holdings`);
}

export function listAccountConditionOrders(
  agentId: string,
): Promise<{ account_id: string; condition_orders: ConditionOrderInfo[] }> {
  return apiGet(`/api/accounts/${encodeURIComponent(agentId)}/condition-orders`);
}

// ---------- 结算产物域（spec-01 §2.5：trades / settlement_log） ----------

export interface TradeInfo {
  id: string;
  account_id: string;
  order_id: string;
  symbol: string;
  side: "buy" | "sell";
  qty: string;
  price: string;
  amount: string;
  fee_total: string;
  commission: string;
  stamp_tax: string;
  transfer_fee: string;
  trade_time: string;
  basis_used: string;
  quality: string;
  settle_date: string;
  reason: string;
  strategy_version_no: string;
}

export interface SettlementInfo {
  id: string;
  settle_key: string;
  trade_date: string;
  account_id: string;
  agent_name: string;
  granularity_used: Record<string, string>;
  status: string;
  created_at: string;
}

export function listAccountTrades(
  agentId: string,
  settleDate?: string,
): Promise<{ account_id: string; trades: TradeInfo[] }> {
  const q = settleDate ? `?settle_date=${encodeURIComponent(settleDate)}` : "";
  return apiGet(`/api/accounts/${encodeURIComponent(agentId)}/trades${q}`);
}

export function listAccountSettlements(
  agentId: string,
): Promise<{ account_id: string; settlements: SettlementInfo[] }> {
  return apiGet(`/api/accounts/${encodeURIComponent(agentId)}/settlements`);
}

// ---------- 日报域（spec-04 §5.2 daily_reports / spec-06 §6.6 日报中心数据源） ----------

export interface ReportTimelineEntry {
  trade_date: string;
  latest_version: number;
  status: "normal" | "absent" | "resend";
  latest_created_ts: string;
}

export interface ReportDataSectionSummary {
  cash: string | null;
  nav: string | null;
  total_pnl: string | null;
  today_pnl: string | null;
}

export interface ReportDataSectionSettlement {
  done: boolean;
  status: string | null;
  granularity_used: Record<string, string>;
}

export interface ReportDataSectionAnnotations {
  degraded: string[];
  unsettled: boolean;
  notes: string[];
}

/** data_section 只读视图（快照/事件明细以 merged_markdown 为准渲染；本类型按需取摘要角标） */
export interface ReportDataSection {
  schema_version: string;
  account_id: string;
  trade_date: string;
  summary: ReportDataSectionSummary;
  settlement: ReportDataSectionSettlement;
  annotations: ReportDataSectionAnnotations;
}

export interface ReportVersion {
  id: string;
  trade_date: string;
  version: number;
  status: "normal" | "absent" | "resend";
  narrative: string;
  merged_markdown: string;
  data_section: ReportDataSection;
  created_ts: string;
}

export function listReportTimeline(
  agentId: string,
): Promise<{ account_id: string; reports: ReportTimelineEntry[] }> {
  return apiGet(`/api/accounts/${encodeURIComponent(agentId)}/reports`);
}

export function fetchReportVersions(
  agentId: string,
  tradeDate: string,
): Promise<{ account_id: string; trade_date: string; versions: ReportVersion[] }> {
  return apiGet(`/api/accounts/${encodeURIComponent(agentId)}/reports/${encodeURIComponent(tradeDate)}`);
}

export function updateReportNarrative(
  agentId: string,
  tradeDate: string,
  version: number,
  narrative: string,
): Promise<{ ok: true; report: { version: number; unchanged: boolean } }> {
  return apiPatch(
    `/api/accounts/${encodeURIComponent(agentId)}/reports/${encodeURIComponent(tradeDate)}/versions/${version}/narrative`,
    { narrative },
  );
}

/** 单篇导出下载地址（同源会话 Cookie 直通，spec-06 §6.6 P3 基础版） */
export function reportExportUrl(agentId: string, tradeDate: string, version?: number): string {
  const q = version !== undefined ? `?version=${version}` : "";
  return `/api/accounts/${encodeURIComponent(agentId)}/reports/${encodeURIComponent(tradeDate)}/export${q}`;
}

export function fetchPushSettings(
  agentId: string,
): Promise<{ agent_id: string; notify_daily: boolean }> {
  return apiGet(`/api/accounts/${encodeURIComponent(agentId)}/push-settings`);
}

export function updatePushSettings(
  agentId: string,
  notifyDaily: boolean,
): Promise<{ agent_id: string; notify_daily: boolean }> {
  return apiPatch(`/api/accounts/${encodeURIComponent(agentId)}/push-settings`, {
    notify_daily: notifyDaily,
  });
}

// ---------- 审批域（spec-04 §4 / spec-06 §6.10 审批中心） ----------

export interface ApprovalInfo {
  id: string;
  type: string;
  type_label: string;
  agent_id: string;
  payload: Record<string, unknown>;
  content_hash: string;
  status: "pending" | "approved" | "rejected" | "expired" | "withdrawn";
  status_label: string;
  decided_by: string;
  decided_ts: string;
  reason: string;
  expires_ts: string;
  close_note: string;
  result_ref: string;
  created_ts: string;
}

export function listApprovals(params: {
  agentId?: string;
  status?: string;
  limit?: number;
}): Promise<{ approvals: ApprovalInfo[] }> {
  const q = new URLSearchParams();
  if (params.agentId) q.set("agent_id", params.agentId);
  if (params.status) q.set("status", params.status);
  if (params.limit) q.set("limit", String(params.limit));
  const s = q.toString();
  return apiGet(`/api/approvals${s ? `?${s}` : ""}`);
}

export function fetchApproval(approvalId: string): Promise<{ approval: ApprovalInfo }> {
  return apiGet(`/api/approvals/${encodeURIComponent(approvalId)}`);
}

export function submitApproval(body: {
  type: string;
  agent_id: string;
  payload: Record<string, unknown>;
  reason: string;
  cooldown_exempt?: boolean;
}): Promise<{ ok: boolean; reason?: string; detail?: string; approval?: ApprovalInfo }> {
  return apiPost("/api/approvals", body);
}

export function decideApproval(
  approvalId: string,
  decision: "approved" | "rejected",
  reason?: string,
): Promise<{ ok: boolean; reason?: string; detail?: string; approval?: ApprovalInfo }> {
  return apiPatch(`/api/approvals/${encodeURIComponent(approvalId)}/decision`, {
    decision,
    reason: reason ?? "",
  });
}

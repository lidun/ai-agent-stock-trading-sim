import { apiGet, apiPost } from "./client";

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

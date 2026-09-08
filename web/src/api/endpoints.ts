import { apiDelete, apiGet, apiPatch, apiPost } from "./client";

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

export interface TrialProgress {
  agent_id: string;
  agent_status: string;
  trial_status: string | null;
  window_days: number;
  replay_status: string;
  sessions: string[];
  sessions_done: number;
  settle_days: number;
  condition_orders: number;
  abnormal_dates: string[];
  gates: { window_ok: boolean; attempt_ok: boolean; abnormal_ok: boolean };
  reasons: string[];
}

export interface TrialFinishResult {
  archive_id: string;
  agent: { id: string; name: string; role: string; status: string };
  trial_account_id: string;
  snapshot: Record<string, unknown>;
}

export function listAgents(): Promise<{ agents: AgentInfo[] }> {
  return apiGet("/api/agents");
}

export function trialProgress(agentId: string): Promise<TrialProgress> {
  return apiGet(`/api/agents/${encodeURIComponent(agentId)}/trial/progress`);
}

export function finishTrial(
  agentId: string,
  decision: "launch" | "reject",
  verdict: string,
): Promise<TrialFinishResult> {
  return apiPost(`/api/agents/${encodeURIComponent(agentId)}/trial/finish`, {
    decision,
    verdict,
  });
}

// ---------- 用户直控（spec-06 §6.3 → spec-01 直控接口） ----------

export type ControlOp = "pause_buy" | "halt" | "resume";

export interface ControlResult {
  agent_id: string;
  op: ControlOp;
  account: AccountInfo;
  from: string;
  to: string;
}

export interface EmergencySellResult {
  agent_id: string;
  account_id: string;
  holdings: number;
  orders: { symbol: string; qty: number; id: string }[];
  blocked_halted: boolean;
}

export function controlAgent(agentId: string, op: ControlOp): Promise<ControlResult> {
  return apiPatch(`/api/agents/${encodeURIComponent(agentId)}/control`, { op });
}

export function emergencySellAll(agentId: string): Promise<EmergencySellResult> {
  return apiPost(`/api/agents/${encodeURIComponent(agentId)}/control/sell-all`);
}

// ---------- 全局直控（spec-06 §6.3 全局层，作用于全部运行中策略 Agent） ----------

export interface BatchControlResult {
  op: ControlOp;
  target: string;
  applied: { agent_id: string; account_id: string; from: string; to: string }[];
  skipped: { agent_id: string; reason: string }[];
  applied_count: number;
  skipped_count: number;
}

export interface BatchSellResult {
  agents: {
    agent_id: string;
    account_id: string;
    holdings: number;
    orders: { symbol: string; qty: number; id: string }[];
    blocked_halted: boolean;
  }[];
  agents_count: number;
  total_holdings: number;
  total_orders: number;
}

export function batchControl(op: ControlOp): Promise<BatchControlResult> {
  return apiPost("/api/control/batch", { op });
}

export function batchSellAll(): Promise<BatchSellResult> {
  return apiPost("/api/control/sell-all");
}

// ---------- 冻结证券（spec-06 §6.3 逐票冻结买入/解除，常驻清单） ----------

export interface FrozenSecurity {
  id: string;
  agent_id: string;
  symbol: string;
  reason: string;
  created_by: string;
  created_ts: string;
}

export interface FreezeResult {
  agent_id: string;
  account_id: string;
  symbol: string;
  cancelled_buy_orders: number;
}

export function listFrozen(agentId?: string): Promise<{ frozen: FrozenSecurity[] }> {
  const q = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : "";
  return apiGet(`/api/frozen${q}`);
}

export function freezeSecurity(
  agentId: string,
  symbol: string,
  reason: string,
): Promise<FreezeResult> {
  return apiPost(`/api/agents/${encodeURIComponent(agentId)}/frozen`, { symbol, reason });
}

export function unfreezeSecurity(
  agentId: string,
  symbol: string,
): Promise<{ agent_id: string; symbol: string; removed: number }> {
  return apiDelete(
    `/api/agents/${encodeURIComponent(agentId)}/frozen/${encodeURIComponent(symbol)}`,
  );
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
  role: "main" | "trial" | "validation";
  parent_agent_id: string;
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

// ---------- 知识库（spec-05 §3 / spec-06 §6.7） ----------

export type KbType = "positive" | "pitfall";
export type KbStatus = "observing" | "validating" | "valid" | "invalid" | "sealed";

export interface KbEntry {
  id: string;
  type: KbType;
  name: string;
  description: string;
  computable_spec: Record<string, unknown>;
  severity: "" | "high" | "mid" | "low";
  env_scope: string;
  status: KbStatus;
  invalid_reason: string;
  sealed_reason: string;
  source: string;
  created_by: string;
  origin_agent: string;
  review_gate1_ref: { passed: boolean; checks?: string[]; ts?: string };
  review_gate2_ref: Record<string, unknown>;
  deleted_ts: string;
  created_ts: string;
  updated_ts: string;
  type_label: string;
  status_label: string;
  stats?: KbStatsRow[];
}

export interface KbStatsRow {
  id: string;
  kb_id: string;
  env_bucket: string;
  sample_n: number;
  win_rate: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  expectancy: number | null;
  intercept_n: number;
  exception_n: number;
  stale_n: number;
  dispatch_n: number;
  window_days: number | null;
  note: string;
  updated_ts: string;
}

export function listKb(params?: {
  type?: KbType;
  status?: KbStatus;
  source?: string;
  kw?: string;
  includeDeleted?: boolean;
  limit?: number;
}): Promise<{ entries: KbEntry[] }> {
  const q = new URLSearchParams();
  if (params?.type) q.set("type", params.type);
  if (params?.status) q.set("status", params.status);
  if (params?.source) q.set("source", params.source);
  if (params?.kw) q.set("kw", params.kw);
  if (params?.includeDeleted) q.set("include_deleted", "true");
  if (params?.limit) q.set("limit", String(params.limit));
  const s = q.toString();
  return apiGet(`/api/kb${s ? `?${s}` : ""}`);
}

export function fetchKb(kbId: string): Promise<{ entry: KbEntry }> {
  return apiGet(`/api/kb/${encodeURIComponent(kbId)}`);
}

export function listKbStats(kbId?: string): Promise<{ stats: KbStatsRow[] }> {
  const q = kbId ? `?kb_id=${encodeURIComponent(kbId)}` : "";
  return apiGet(`/api/kb/stats${q}`);
}

export function createKb(body: {
  name: string;
  type: KbType;
  description?: string;
  computable_spec?: Record<string, unknown>;
  severity?: string;
  env_scope?: string;
  source?: string;
  origin_agent?: string;
}): Promise<{ entry: KbEntry }> {
  return apiPost("/api/kb", body);
}

export function updateKb(
  kbId: string,
  body: {
    name?: string;
    description?: string;
    env_scope?: string;
    severity?: string;
    note?: string;
  },
): Promise<{ entry: KbEntry }> {
  return apiPatch(`/api/kb/${encodeURIComponent(kbId)}`, body);
}

export function transitionKb(
  kbId: string,
  action: "start_validation" | "approve_valid" | "invalidate" | "seal",
  note: string,
): Promise<{ entry: KbEntry }> {
  return apiPost(`/api/kb/${encodeURIComponent(kbId)}/transition`, { action, note });
}

export function deleteKb(kbId: string, note?: string): Promise<{ entry: KbEntry }> {
  return apiPost(`/api/kb/${encodeURIComponent(kbId)}/delete`, { note: note ?? "" });
}

export function restoreKb(kbId: string, note?: string): Promise<{ entry: KbEntry }> {
  return apiPost(`/api/kb/${encodeURIComponent(kbId)}/restore`, { note: note ?? "" });
}

export function upsertKbStats(
  kbId: string,
  body: Partial<KbStatsRow> & { env_bucket: string; note?: string },
): Promise<{ stats: KbStatsRow }> {
  return apiPost(`/api/kb/${encodeURIComponent(kbId)}/stats`, body);
}

// ---------- 策略分析（spec-06 §6.4 P2：资金曲线/指标卡/策略演进） ----------

export type CurveRange = "all" | "1m" | "3m";
export type AccountRole = "main" | "trial" | "validation";

export interface EquityPoint {
  trade_date: string;
  nav: number;
  return_pct: number;
}
export interface BenchPoint {
  trade_date: string;
  close: number;
  return_pct: number;
}
export interface EquitySeriesAccount {
  account_id: string;
  role: AccountRole;
  label: string;
  active_version_no: string;
  created_ts: string;
  status: string;
  points: EquityPoint[];
  last_return_pct: number | null;
}
export interface EquityCurve {
  agent_id: string;
  range: string;
  series: EquitySeriesAccount[];
  benchmark: { available: boolean; reason: string; points: BenchPoint[] };
}

export interface SignalStats {
  n: number;
  done: number;
  win_n: number;
  tie_n: number;
  early_n: number;
  win_rate_pct: number | null;
  avg_fwd_return_pct: number | null;
  avg_excess_pct: number | null;
  note: string;
}
export interface StrategyMetrics {
  agent_id: string;
  as_of: string;
  cum_return_pct: number | null;
  max_drawdown_pct: number | null;
  settle_days: number;
  nav_last: number | null;
  signal: SignalStats;
}

export interface EvolutionTrial {
  window_days: number;
  replay_status: string;
}
export interface EvolutionLedger {
  account_id: string;
  role: AccountRole;
  label: string;
  status: string;
  active_version_no: string;
  created_ts: string;
  report_days: number;
  first_report_date: string;
  last_report_date: string;
  first_nav: number | null;
  nav_last: number | null;
  return_pct: number | null;
  trial: EvolutionTrial | null;
}
export interface EvolutionArchive {
  account_id: string;
  decision: "launch" | "reject";
  verdict: string;
  archived_ts: string;
  end_nav: number | null;
  end_total_pnl: number | null;
  replay_window_days: number | null;
  replay_sessions: number | null;
  settle_days: number | null;
  orders: number | null;
  trades: number | null;
  holdings: number | null;
}
export interface StrategyEvolution {
  agent_id: string;
  note: string;
  ledgers: EvolutionLedger[];
  archive: EvolutionArchive | null;
  generated_ts: string;
}

export function fetchEquityCurve(agentId: string, range: CurveRange = "all"): Promise<EquityCurve> {
  return apiGet<EquityCurve>(`/api/agents/${encodeURIComponent(agentId)}/equity-curve?range=${range}`);
}

export function fetchStrategyMetrics(agentId: string): Promise<StrategyMetrics> {
  return apiGet<StrategyMetrics>(`/api/agents/${encodeURIComponent(agentId)}/metrics`);
}

export function fetchEvolution(agentId: string): Promise<StrategyEvolution> {
  return apiGet<StrategyEvolution>(`/api/agents/${encodeURIComponent(agentId)}/evolution`);
}

// ---------- 策略理念 · 章程只读（spec-06 §6.4 理念区块；spec-05 §4.1 双层结构） ----------

export interface CharterVersionSummary {
  version_no: string;
  active: boolean;
  locked: boolean;
  charter_hash: string;
  note: string;
  created_ts: string;
}
export interface CharterFull extends CharterVersionSummary {
  core_belief: string;
  layers: Record<string, unknown>;
}
export interface StrategyProfile {
  agent_id: string;
  active: CharterFull | null;
  versions: CharterVersionSummary[];
  has_capability_packs: boolean;
}
export interface CharterVersionDetail {
  agent_id: string;
  version: CharterFull;
}

export function fetchStrategyProfile(agentId: string): Promise<StrategyProfile> {
  return apiGet<StrategyProfile>(`/api/agents/${encodeURIComponent(agentId)}/strategy-profile`);
}

export function fetchCharterVersion(
  agentId: string,
  versionNo: string,
): Promise<CharterVersionDetail> {
  return apiGet<CharterVersionDetail>(
    `/api/agents/${encodeURIComponent(agentId)}/strategy-profile/versions/${encodeURIComponent(versionNo)}`,
  );
}

// ---------- 卖出跟踪只读（spec-06 §6.4 P3 文字+表格，#15；spec-01 §8.1） ----------

export interface ExitTrackingItem {
  id: string;
  account_id: string;
  role: string;
  sell_trade_id: string;
  symbol: string;
  sell_date: string;
  sell_price: number | null;
  qty: number | null;
  sell_reason: string;
  status: "tracking" | "done";
  sessions_done: number;
  track_end_date: string;
  fwd_return_pct: number | null;
  bench_return_pct: number | null;
  excess_pct: number | null;
  period_high: number | null;
  period_low: number | null;
  conclusion: string;
  is_loss_case: boolean;
  quality: string;
  created_ts: string;
  done_ts: string;
}
export interface ExitTrackingList {
  agent_id: string;
  total: number;
  tracking: number;
  done: number;
  items: ExitTrackingItem[];
}

export function fetchExitTrackings(
  agentId: string,
  status?: "tracking" | "done",
): Promise<ExitTrackingList> {
  const q = status ? `?status=${status}` : "";
  return apiGet<ExitTrackingList>(`/api/agents/${encodeURIComponent(agentId)}/exit-trackings${q}`);
}

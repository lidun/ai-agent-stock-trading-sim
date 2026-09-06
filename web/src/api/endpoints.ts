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

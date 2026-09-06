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

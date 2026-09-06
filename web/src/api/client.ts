/** API 客户端：同源 /api 访问（开发期经 Vite 反代、生产经 Nginx 反代）。
 *  会话 Cookie 由 core 下发（HttpOnly）；所有写请求携带 X-CSRF-Token（spec-06 §3）。 */

export const SESSION_COOKIE = "aat_session";
export const CSRF_COOKIE = "aat_csrf";

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function readCookie(name: string): string | null {
  const m = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return m ? decodeURIComponent(m[1]) : null;
}

export function getCsrfToken(): string | null {
  return readCookie(CSRF_COOKIE);
}

export async function ensureCsrf(): Promise<void> {
  if (!getCsrfToken()) {
    await apiGet("/api/auth/csrf");
  }
}

type ApiOptions = {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
};

async function request<T>(path: string, opts: ApiOptions = {}): Promise<T> {
  const method = (opts.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(opts.headers ?? {}),
  };
  let body: string | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const unsafe = !["GET", "HEAD", "OPTIONS"].includes(method);
  const csrf = getCsrfToken();
  if (unsafe && csrf) headers["X-CSRF-Token"] = csrf;

  let resp: Response;
  try {
    resp = await fetch(path, {
      method,
      headers,
      body,
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(0, "无法连接后端服务");
  }
  if (!resp.ok) {
    let detail = `请求失败 (${resp.status})`;
    try {
      const data = (await resp.json()) as { detail?: string };
      if (data.detail) detail = data.detail;
    } catch {
      /* 非 JSON 响应体 */
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export function apiGet<T>(path: string): Promise<T> {
  return request<T>(path);
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "POST", body });
}

import { useEffect, useState } from "react";

export type ConnState = "connecting" | "online" | "offline";

const RECONNECT_BASE_MS = 1500;
const RECONNECT_MAX_MS = 20000;

/** 与 core 的 WS 心跳连接（spec-06 §8）：断线自动重连 + 状态暴露给顶栏。
 *  在途重连尝试由浏览器决定何时重试（指数退避、随机抖动）。 */
export function useConnection(): ConnState {
  const [state, setState] = useState<ConnState>("connecting");

  useEffect(() => {
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempts = 0;
    let disposed = false;

    const url = (): string => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      return `${proto}://${location.host}/ws`;
    };

    const schedule = () => {
      if (disposed) return;
      const delay = Math.min(
        RECONNECT_BASE_MS * 2 ** Math.min(attempts, 6),
        RECONNECT_MAX_MS,
      );
      timer = setTimeout(connect, delay);
    };

    const connect = () => {
      if (disposed) return;
      attempts += 1;
      setState("connecting");
      try {
        ws = new WebSocket(url());
      } catch {
        schedule();
        return;
      }
      ws.onopen = () => {
        attempts = 0;
        setState("online");
      };
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(String(ev.data)) as { type?: string };
          if (data.type === "ping") ws?.send('{"type":"pong"}');
        } catch {
          /* 忽略非 JSON 帧 */
        }
      };
      ws.onclose = () => {
        setState("offline");
        schedule();
      };
      ws.onerror = () => {
        try {
          ws?.close();
        } catch {
          /* noop */
        }
      };
    };

    connect();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
    };
  }, []);

  return state;
}

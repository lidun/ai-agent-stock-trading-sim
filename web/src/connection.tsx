import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

export type ConnState = "connecting" | "online" | "offline";

/** 站内通知事件（type=conv，spec-06 §8） */
export interface ConvEvent {
  type: "conv";
  event: "message_new" | "message_status" | "conversation_read";
  conv_id: string;
  message?: {
    id: string;
    direction: string;
    status: string;
    conv_id: string;
    ts: string;
    body?: string;
  };
  updated?: number;
}

interface ConnectionValue {
  state: ConnState;
  /** 每次重新连上自增——页面据此重拉当前数据（spec-06 §3 重连后 refetch） */
  epoch: number;
  subscribe: (listener: (ev: ConvEvent) => void) => () => void;
}

const RECONNECT_BASE_MS = 1500;
const RECONNECT_MAX_MS = 20000;

const ConnectionContext = createContext<ConnectionValue>({
  state: "connecting",
  epoch: 0,
  subscribe: () => () => undefined,
});

export function ConnectionProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<ConnState>("connecting");
  const [epoch, setEpoch] = useState(0);
  const listeners = useRef(new Set<(ev: ConvEvent) => void>());

  const subscribe = useCallback((fn: (ev: ConvEvent) => void) => {
    listeners.current.add(fn);
    return () => {
      listeners.current.delete(fn);
    };
  }, []);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempts = 0;
    let disposed = false;

    const publish = (ev: ConvEvent) => {
      listeners.current.forEach((fn) => {
        try {
          fn(ev);
        } catch {
          /* 监听器异常不影响通道 */
        }
      });
    };

    const schedule = () => {
      if (disposed) return;
      const delay = Math.min(RECONNECT_BASE_MS * 2 ** Math.min(attempts, 6), RECONNECT_MAX_MS);
      timer = setTimeout(connect, delay);
    };

    const connect = () => {
      if (disposed) return;
      attempts += 1;
      setState("connecting");
      try {
        const proto = location.protocol === "https:" ? "wss" : "ws";
        ws = new WebSocket(`${proto}://${location.host}/ws`);
      } catch {
        schedule();
        return;
      }
      ws.onopen = () => {
        attempts = 0;
        setState("online");
        setEpoch((e) => e + 1);
      };
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(String(ev.data)) as { type?: string };
          if (data.type === "pong") return;
          if (data.type === "conv") publish(data as ConvEvent);
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

  const value = useMemo(() => ({ state, epoch, subscribe }), [state, epoch, subscribe]);
  return <ConnectionContext.Provider value={value}>{children}</ConnectionContext.Provider>;
}

export function useConnection(): ConnectionValue {
  return useContext(ConnectionContext);
}

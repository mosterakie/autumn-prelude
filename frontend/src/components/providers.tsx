"use client";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api, isDemo, mutate, setCsrf } from "@/lib/api";
import type { Identity, User } from "@/lib/contracts";
import { AuthForm } from "./auth";
import { Modal, Notice, Spinner } from "./ui";
interface Session {
  user: User | null;
  loading: boolean;
  error: Error | null;
  ownerReady: boolean;
  serverNow: () => number;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
  openLogin: () => void;
  toast: (message: string) => void;
}
const Context = createContext<Session | null>(null);
export function useSession() {
  const value = useContext(Context);
  if (!value) throw new Error("Session provider missing");
  return value;
}
function SessionProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient(),
    query = useQuery({
      queryKey: ["identity"],
      queryFn: () => api<Identity>("/auth/me"),
      staleTime: 0,
      retry: false,
      refetchInterval: 30000,
    });
  const [login, setLogin] = useState(false),
    [message, setMessage] = useState(""),
    [clock, setClock] = useState(Date.now());
  const time = useRef({ server: Date.now(), local: Date.now() }),
    previous = useRef<string | null>(null),
    channel = useRef<BroadcastChannel | null>(null);
  const user = query.data?.user || null;
  const clear = useCallback(() => {
    setCsrf(null);
    client.clear();
    client.setQueryData(["identity"], {
      user: null,
      csrf_token: null,
      server_time: new Date().toISOString(),
    });
    window.dispatchEvent(new Event("autumn:logout"));
  }, [client]);
  useEffect(() => {
    if (query.data) {
      setCsrf(query.data.csrf_token);
      time.current = {
        server: Date.parse(query.data.server_time),
        local: Date.now(),
      };
      const uid = query.data.user?.id || null;
      if (previous.current !== uid) {
        client.removeQueries({
          predicate: (q) => q.queryKey[0] !== "identity",
        });
        window.dispatchEvent(new Event("autumn:logout"));
        previous.current = uid;
      }
    }
  }, [query.data, client]);
  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 1000);
    const bc = new BroadcastChannel("autumn-prelude-session");
    channel.current = bc;
    bc.onmessage = (e) => {
      if (e.data === "logout") clear();
    };
    const expired = () => clear();
    window.addEventListener("autumn:expired", expired);
    return () => {
      clearInterval(timer);
      bc.close();
      window.removeEventListener("autumn:expired", expired);
    };
  }, [clear]);
  useEffect(() => {
    if (!message) return;
    const t = setTimeout(() => setMessage(""), 4500);
    return () => clearTimeout(t);
  }, [message]);
  const serverNow = () => time.current.server + (clock - time.current.local);
  const ownerReady =
    !!user &&
    user.role === "owner" &&
    !!user.step_up_expires_at &&
    Date.parse(user.step_up_expires_at) > serverNow();
  const wasOwnerReady = useRef(false);
  useEffect(() => {
    if (wasOwnerReady.current && !ownerReady) {
      client.cancelQueries({
        predicate: (q) =>
          !["identity", "public"].includes(String(q.queryKey[0])),
      });
      client.removeQueries({
        predicate: (q) =>
          !["identity", "public"].includes(String(q.queryKey[0])),
      });
      window.dispatchEvent(new Event("autumn:logout"));
    }
    wasOwnerReady.current = ownerReady;
  }, [ownerReady, client]);
  const refresh = async () => {
    await query.refetch();
  };
  const logout = async () => {
    await mutate("/auth/logout");
    clear();
    channel.current?.postMessage("logout");
    setMessage("已退出，个人内容缓存已清除。");
  };
  return (
    <Context.Provider
      value={{
        user,
        loading: query.isPending,
        error: query.error,
        ownerReady,
        serverNow,
        refresh,
        logout,
        openLogin: () => setLogin(true),
        toast: setMessage,
      }}
    >
      {children}
      {login && (
        <Modal title="欢迎回到秋序" onClose={() => setLogin(false)}>
          <AuthForm kind="login" onSuccess={() => setLogin(false)} />
        </Modal>
      )}
      {message && (
        <div className="toast" role="status">
          {message}
        </div>
      )}
    </Context.Provider>
  );
}
export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            retry: false,
            staleTime: 15000,
            refetchOnWindowFocus: true,
          },
          mutations: { retry: false },
        },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <SessionProvider>{children}</SessionProvider>
    </QueryClientProvider>
  );
}
export function AuthGate({
  children,
  owner = false,
}: {
  children: ReactNode;
  owner?: boolean;
}) {
  const session = useSession();
  if (session.loading) return <Spinner />;
  if (session.error)
    return <Notice error>账号状态无法读取。请检查后端连接后刷新。</Notice>;
  if (!session.user)
    return (
      <div className="gate panel">
        <p className="eyebrow">YOUR OWN LITTLE CORNER</p>
        <h2>先认识一下，再开始聊。</h2>
        <p>公开页面可以自由阅读；登录后，可留下想法或与助手对话。</p>
        <AuthForm kind="login" />
      </div>
    );
  if (session.user.status === "disabled")
    return <Notice error>账号当前不可用，请联系站长。</Notice>;
  if (!session.user.verified_at)
    return (
      <div className="gate panel">
        <h2>还差一次邮箱验证</h2>
        <p>请先验证邮箱，再使用留言和 AI。</p>
        <AuthForm kind="resend-verification" />
      </div>
    );
  if (owner && session.user.role !== "owner")
    return (
      <Notice error>这一页是站长的私人工作台。你仍然可以使用公开问答。</Notice>
    );
  if (owner && !session.ownerReady)
    return (
      <div className="gate panel">
        <h2>开启私人工作台</h2>
        <p>输入验证器中的六位验证码，本次验证有效期为 15 分钟。</p>
        <AuthForm kind="step-up" />
      </div>
    );
  return (
    <div key={`${session.user.id}:${owner ? "owner" : "public"}`}>
      {children}
    </div>
  );
}
export function DemoBadge() {
  return isDemo ? (
    <span className="demo-badge">界面预览 · 示例内容</span>
  ) : null;
}

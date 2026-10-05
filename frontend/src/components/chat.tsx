"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  BookOpen,
  Globe,
  Plus,
  Square,
  Trash2,
  Pencil,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import { api, isDemo, mutate, newKey } from "@/lib/api";
import { subscribeDemo } from "@/lib/demo";
import type {
  AcceptedRun,
  Action,
  Ask,
  Citation,
  Conversation,
  Message,
  Mode,
  Page,
  Quota,
  Run,
  RunStatus,
} from "@/lib/contracts";
import { terminal } from "@/lib/contracts";
import {
  applySnapshot,
  hideSources,
  normalizeRun,
  normalizeSnapshot,
  safeExternal,
} from "@/lib/safety";
import { useSession } from "./providers";
import { AssistantAvatar } from "./avatar";
import { ActionPreview } from "./actions";
import { Button, Empty, Markdown, Modal, Notice } from "./ui";
export function Chat({
  conversationId,
  owner = false,
}: {
  conversationId?: string;
  owner?: boolean;
}) {
  const session = useSession(),
    client = useQueryClient(),
    router = useRouter(),
    search = useSearchParams(),
    mode: Mode = owner ? "owner" : "public",
    base = owner ? "/admin/chat" : "/chat",
    uid = session.user!.id;
  const list = useQuery({
    queryKey: ["conversations", uid, mode],
    queryFn: () => api<Page<Conversation>>(`/conversations?mode=${mode}`),
  });
  const history = useQuery({
    queryKey: ["messages", uid, mode, conversationId],
    queryFn: () =>
      api<Page<Message>>(`/conversations/${conversationId}/messages`),
    enabled: !!conversationId,
  });
  const current = useQuery({
    queryKey: ["conversation", uid, mode, conversationId],
    queryFn: () => api<Conversation>(`/conversations/${conversationId}`),
    enabled: !!conversationId,
  });
  const quota = useQuery({
    queryKey: ["quota", uid],
    queryFn: () => api<Quota>("/me/quota"),
  });
  const [draft, setDraft] = useState(""),
    [localMessages, setMessages] = useState<Message[]>([]),
    [runId, setRunId] = useState<string | null>(null),
    [status, setStatus] = useState<RunStatus | undefined>(),
    [run, setRun] = useState<Run | null>(null),
    [busy, setBusy] = useState(false),
    [focused, setFocused] = useState(false),
    [tool, setTool] = useState(""),
    [working, setWorking] = useState(false),
    [error, setError] = useState(""),
    [connection, setConnection] = useState(""),
    [citations, setCitations] = useState<Citation[] | null>(null),
    [sidebar, setSidebar] = useState(true),
    [searchMode, setSearchMode] = useState<Ask["search_mode"]>("site"),
    [inputAnswer, setInputAnswer] = useState(""),
    [streamEpoch, setStreamEpoch] = useState(0),
    [olderCursor, setOlderCursor] = useState<string | null | undefined>(),
    [moreConversations, setMoreConversations] = useState<Conversation[]>([]),
    [listCursor, setListCursor] = useState<string | null | undefined>(),
    [rename, setRename] = useState<Conversation | null>(null),
    [renameTitle, setRenameTitle] = useState(""),
    [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null);
  const pending = useRef<{
      body: Ask;
      key: string;
      creationKey: string;
    } | null>(null),
    cursor = useRef(""),
    cursorRun = useRef<string | null>(null),
    cleanup = useRef<(() => void) | null>(null),
    scroll = useRef<HTMLDivElement>(null),
    conversationRef = useRef<string | null>(conversationId || null),
    mounted = useRef(true);
  const invalidate = () => {
    client.invalidateQueries({ queryKey: ["quota", uid] });
    client.invalidateQueries({ queryKey: ["conversations", uid, mode] });
  };
  const openCitations = async (values: Citation[]) => {
    try {
      const fresh = isDemo
        ? values
        : await Promise.all(
            values.map((c) =>
              api<Citation>(`/citations/${encodeURIComponent(c.id)}`),
            ),
          );
      setCitations(fresh);
    } catch (e) {
      setCitations(null);
      setError(`来源暂时无法读取：${(e as Error).message}`);
    }
  };
  useEffect(() => {
    mounted.current = true;
    if (window.matchMedia("(max-width: 680px)").matches) setSidebar(false);
    const close = () => {
      cleanup.current?.();
      setMessages([]);
      setCitations(null);
      setRun(null);
    };
    window.addEventListener("autumn:logout", close);
    return () => {
      mounted.current = false;
      cleanup.current?.();
      window.removeEventListener("autumn:logout", close);
    };
  }, []);
  useEffect(() => {
    if (history.data)
      setMessages((previous) =>
        history.data.items.reduce(applySnapshot, previous),
      );
  }, [history.data]);
  useEffect(() => {
    if (current.data?.active_run_id) {
      api<Run>(`/runs/${current.data.active_run_id}`)
        .then(normalizeRun)
        .then((r) => {
          if (mounted.current) {
            setRun(r);
            setStatus(r.status);
            if (r.message) setMessages((m) => applySnapshot(m, r.message!));
            if (!terminal(r.status)) setRunId(r.run_id);
          }
        })
        .catch((e) => setError((e as Error).message));
    }
  }, [current.data]);
  useEffect(() => {
    scroll.current?.scrollIntoView({ behavior: "auto", block: "end" });
  }, [localMessages, tool]);
  useEffect(() => {
    if (!runId) return;
    if (cursorRun.current !== runId) {
      cursor.current = "";
      cursorRun.current = runId;
    }
    let stopped = false,
      retryTimer: ReturnType<typeof setTimeout> | null = null,
      retries = 0;
    let source: EventSource | null = null,
      demoClose: (() => void) | null = null;
    const refreshRun = async () => {
      const r = normalizeRun(await api<Run>(`/runs/${runId}`));
      if (stopped) return;
      setRun(r);
      setStatus(r.status);
      if (r.message) setMessages((m) => applySnapshot(m, r.message!));
      return r;
    };
    const handle = (name: string, data: Record<string, any>, sequence = "") => {
      if (stopped) return;
      if (sequence) {
        if (
          cursor.current &&
          /^\d+$/.test(sequence) &&
          Number(sequence) <= Number(cursor.current)
        )
          return;
        cursor.current = sequence;
      }
      if (name === "message.snapshot")
        setMessages((m) =>
          applySnapshot(m, normalizeSnapshot(data.message || data)),
        );
      else if (name === "run.status") {
        setStatus(data.status);
        if (
          ["waiting_input", "waiting_auth", "waiting_approval"].includes(
            data.status,
          )
        )
          refreshRun().catch((e) => setError((e as Error).message));
      } else if (name === "tool.started" || name === "knowledge.processing") {
        setTool(data.display_name || `资料处理中 · ${data.phase || "排队"}`);
        setWorking(true);
      } else if (name === "tool.finished") {
        setTool(data.result_summary || "资料已查阅");
        setWorking(false);
      } else if (name === "action.proposed") {
        setRun((r) => ({
          ...(r || {
            run_id: runId,
            conversation_id: conversationRef.current!,
            status: "waiting_approval",
          }),
          pending_actions: [
            ...(r?.pending_actions || []).filter((a) => a.id !== data.id),
            data as Action,
          ],
        }));
        setStatus("waiting_approval");
      } else if (name === "action.succeeded") {
        setRun((r) =>
          r
            ? {
                ...r,
                pending_actions: r.pending_actions?.filter(
                  (a) => a.id !== data.action_id,
                ),
              }
            : r,
        );
        session.toast("助手操作已完成。");
        client.invalidateQueries({ queryKey: ["resources"] });
      } else if (name === "source.invalidated") {
        setMessages((m) => hideSources(m, data.message_ids || []));
        setCitations(null);
        setConnection("部分来源已被收回，相关回答已隐藏。");
        client.removeQueries({
          queryKey: ["messages", uid, mode, conversationRef.current],
        });
        api<Page<Message>>(`/conversations/${conversationRef.current}/messages`)
          .then((p) => {
            if (!stopped) setMessages(p.items);
          })
          .catch(() => {});
      } else if (name === "scope.changed") {
        setMessages([]);
        setCitations(null);
        setStatus("waiting_auth");
        source?.close();
        session.refresh();
        setConnection("访问范围发生变化，请重新验证后继续。");
      } else if (name === "error") {
        setError(data.message || "回答出现问题，请查看任务状态。");
        refreshRun().catch(() => {});
      } else if (name === "done") {
        setStatus(data.status);
        setWorking(false);
        source?.close();
        setRunId(null);
        pending.current = null;
        invalidate();
        refreshRun().catch(() => {});
      }
    };
    const connect = () => {
      if (stopped) return;
      source?.close();
      source = new EventSource(
        `/api/runs/${encodeURIComponent(runId)}/events${cursor.current ? `?after=${encodeURIComponent(cursor.current)}` : ""}`,
        { withCredentials: true },
      );
      const events = [
        "run.status",
        "message.snapshot",
        "tool.started",
        "tool.finished",
        "knowledge.processing",
        "action.proposed",
        "action.succeeded",
        "source.invalidated",
        "scope.changed",
        "error",
        "done",
      ];
      events.forEach((name) =>
        source!.addEventListener(name, (event) => {
          if (event instanceof MessageEvent && event.data) {
            try {
              handle(name, JSON.parse(event.data), event.lastEventId);
            } catch {
              setError("事件格式无法识别，请查看任务状态。");
            }
          }
        }),
      );
      source.onopen = () => {
        setConnection("");
        retries = 0;
      };
      source.onerror = async (event) => {
        if (event instanceof MessageEvent) return;
        source?.close();
        if (stopped) return;
        setConnection("连接中断，正在恢复同一任务。不会重新提交问题。");
        try {
          const r = await refreshRun();
          if (r && terminal(r.status)) {
            handle("done", { status: r.status });
            return;
          }
        } catch (e) {
          setError((e as Error).message);
        }
        if (!stopped)
          retryTimer = setTimeout(
            connect,
            Math.min(30000, 1000 * 2 ** retries++),
          );
      };
    };
    if (isDemo)
      demoClose = subscribeDemo(runId, (e) =>
        handle(e.name, e.data, e.sequence),
      );
    else connect();
    const stop = () => {
      stopped = true;
      source?.close();
      demoClose?.();
      if (retryTimer) clearTimeout(retryTimer);
    };
    cleanup.current = stop;
    return stop;
    // Each subscription belongs to one immutable run and account scope.
  }, [runId, uid, mode, streamEpoch]);
  const send = async (retry = false) => {
    if (!draft.trim() && !retry) return;
    if (busy || (status && !terminal(status))) return;
    setBusy(true);
    setError("");
    setTool("");
    setConnection("");
    try {
      if (!retry || !pending.current)
        pending.current = {
          key: newKey(),
          creationKey: newKey(),
          body: {
            conversation_id: conversationRef.current || "",
            client_message_id: newKey(),
            message: draft.trim(),
            resource_ids: search.get("resource")
              ? [search.get("resource")!]
              : [],
            search_mode: searchMode,
          },
        };
      const request = pending.current;
      if (!request.body.conversation_id) {
        const c = await mutate<Conversation>(
          "/conversations",
          { mode },
          "POST",
          request.creationKey,
        );
        request.body.conversation_id = c.id;
        conversationRef.current = c.id;
      }
      const result = await mutate<AcceptedRun>(
        "/ask",
        request.body,
        "POST",
        request.key,
      );
      setMessages((m) =>
        applySnapshot(m, {
          id: request.body.client_message_id,
          role: "user",
          body: request.body.message,
          content_version: 1,
          status: "complete",
          created_at: new Date().toISOString(),
          citations: [],
        }),
      );
      setDraft("");
      setStatus(result.status);
      setRunId(result.run_id);
      client.setQueryData(["quota", uid], result.quota);
      invalidate();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const q = quota.data,
    cooldown =
      q?.cooldown_until && Date.parse(q.cooldown_until) > session.serverNow(),
    active = !!status && !terminal(status),
    quotaBlocked = !!q && (q.remaining === 0 || !!cooldown);
  const starter = owner
    ? [
        "帮我整理这周收藏的网页",
        "查找一个问题的最新资料",
        "先预览，再公开一篇手记",
      ]
    : ["秋序是一个怎样的小站？", "推荐一篇公开手记", "聊聊知识整理与阅读"];
  return (
    <div className={`chat-layout panel ${!sidebar ? "sidebar-hidden" : ""}`}>
      {sidebar && (
        <aside className="chat-sidebar">
          <div className="row between">
            <span className="eyebrow">
              {owner ? "PRIVATE WORKSPACE" : "YOUR CONVERSATIONS"}
            </span>
            <button
              className="icon-button"
              aria-label="收起会话列表"
              onClick={() => setSidebar(false)}
            >
              <PanelLeftClose size={18} />
            </button>
          </div>
          <Link
            href={base}
            onClick={() => {
              cleanup.current?.();
              conversationRef.current = null;
              pending.current = null;
              setMessages([]);
              setRunId(null);
              setStatus(undefined);
              setRun(null);
            }}
            className="button new-chat"
          >
            <Plus size={17} /> 新的对话
          </Link>
          <div className="conversation-list">
            {list.error && <Notice error>会话列表暂时无法加载。</Notice>}
            {[...(list.data?.items || []), ...moreConversations].map((c) => (
              <div
                className={`conversation-item ${c.id === conversationId || c.id === conversationRef.current ? "active" : ""}`}
                key={c.id}
              >
                <Link href={`${base}/${c.id}`}>{c.title}</Link>
                <button
                  className="icon-button"
                  aria-label={`重命名${c.title}`}
                  onClick={() => {
                    setRename(c);
                    setRenameTitle(c.title);
                  }}
                >
                  <Pencil size={13} />
                </button>
                <button
                  className="icon-button"
                  aria-label={`删除${c.title}`}
                  onClick={() => setDeleteTarget(c)}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            ))}
            {!list.data?.items.length && (
              <p className="small muted">故事会从第一句话开始。</p>
            )}
            {(listCursor === undefined
              ? list.data?.next_cursor
              : listCursor) && (
              <Button
                className="compact"
                onClick={async () => {
                  try {
                    const next = await api<Page<Conversation>>(
                      `/conversations?mode=${mode}&cursor=${encodeURIComponent((listCursor === undefined ? list.data?.next_cursor : listCursor)!)}`,
                    );
                    setMoreConversations([...moreConversations, ...next.items]);
                    setListCursor(next.next_cursor);
                  } catch (e) {
                    setError((e as Error).message);
                  }
                }}
              >
                更早的对话
              </Button>
            )}
          </div>
          <div className="sidebar-bottom">
            <BookOpen size={18} />
            <p>
              {owner
                ? "私人知识与公开资料分别处理。联网工具仅供站长使用。"
                : "只引用站长允许公开的资料。你的对话仅自己可见。"}
            </p>
          </div>
        </aside>
      )}
      <section className="chat-main">
        <div className="chat-top">
          <div className="row">
            {!sidebar && (
              <button
                className="icon-button"
                onClick={() => setSidebar(true)}
                aria-label="展开会话列表"
              >
                <PanelLeftOpen size={18} />
              </button>
            )}
            <div>
              <strong>停云 · {owner ? "私人助手" : "秋序助手"}</strong>
              <p className="small muted">
                {isDemo
                  ? "交互演示 · 尚未连接模型"
                  : owner
                    ? "你的资料，你的工作台"
                    : "从公开手记与收藏中，寻找答案"}
              </p>
            </div>
          </div>
          <span className="pill">{owner ? "站长模式" : "站内问答"}</span>
        </div>
        <div
          className="messages"
          aria-live="polite"
          aria-relevant="additions text"
        >
          {(olderCursor === undefined
            ? history.data?.next_cursor
            : olderCursor) && (
            <Button
              className="compact"
              onClick={async () => {
                try {
                  const next = await api<Page<Message>>(
                    `/conversations/${conversationRef.current}/messages?cursor=${encodeURIComponent((olderCursor === undefined ? history.data?.next_cursor : olderCursor)!)}`,
                  );
                  setMessages((previous) => [
                    ...next.items.filter(
                      (m) => !previous.some((old) => old.id === m.id),
                    ),
                    ...previous,
                  ]);
                  setOlderCursor(next.next_cursor);
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              查看更早的消息
            </Button>
          )}
          {!localMessages.length ? (
            <div className="chat-welcome">
              <span className="welcome-flower">✳</span>
              <p className="eyebrow">A LITTLE SPACE TO THINK</p>
              <h1>好奇什么，就从这里聊起。</h1>
              <p className="muted">
                {owner
                  ? "整理资料，寻找线索，也把想法变成可确认的行动。"
                  : "一起翻翻手记，看看收藏，慢慢找到新的线索。"}
              </p>
              <div className="starter-prompts">
                {starter.map((s) => (
                  <button
                    key={s}
                    onClick={() => {
                      setDraft(s);
                      document.getElementById("composer")?.focus();
                    }}
                  >
                    {s}
                    <ArrowUp size={14} />
                  </button>
                ))}
              </div>
            </div>
          ) : (
            localMessages.map((m) => (
              <article key={m.id} className={`message message-${m.role}`}>
                <span className="message-author">
                  {m.role === "user" ? "你" : "停云"}
                </span>
                {m.status === "hidden" ? (
                  <Notice>这段回答的来源权限已变化，内容已隐藏。</Notice>
                ) : (
                  <>
                    <Markdown body={m.body} />
                    {m.citations?.length > 0 && (
                      <button
                        className="citation-button"
                        onClick={() => openCitations(m.citations)}
                      >
                        <BookOpen size={14} />
                        {m.citations.length} 条来源 · 查看引用
                      </button>
                    )}
                    {m.status === "interrupted" && (
                      <p className="small muted">回答已中断</p>
                    )}
                  </>
                )}
              </article>
            ))
          )}
          {tool && (
            <div className="tool-status">
              <Sparkles size={14} />
              {tool}
            </div>
          )}
          {run?.pending_actions?.map((action) => (
            <ActionPreview
              key={action.id}
              action={action}
              onDone={() => {
                session.toast("操作已执行，可继续对话。");
                setRun((r) =>
                  r
                    ? {
                        ...r,
                        pending_actions: r.pending_actions?.filter(
                          (a) => a.id !== action.id,
                        ),
                      }
                    : r,
                );
              }}
            />
          ))}
          {status === "waiting_input" && run?.input_request && (
            <form
              className="inset form-stack"
              onSubmit={async (e) => {
                e.preventDefault();
                try {
                  const r = normalizeRun(
                    await mutate<Run>(`/runs/${run.run_id}/resume`, {
                      input_request_id: run.input_request!.id,
                      answer: inputAnswer,
                    }),
                  );
                  setStatus(r.status);
                  setRunId(r.run_id);
                  setStreamEpoch((n) => n + 1);
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              <label>
                {run.input_request.prompt}
                <input
                  required
                  value={inputAnswer}
                  onChange={(e) => setInputAnswer(e.target.value)}
                />
              </label>
              <Button>补充并继续</Button>
            </form>
          )}
          {status === "waiting_auth" && run && owner && session.ownerReady && (
            <div className="notice">
              <p>站长验证已完成，可以恢复原来的任务。</p>
              <Button
                onClick={async () => {
                  try {
                    const next = normalizeRun(
                      await mutate<Run>(`/runs/${run.run_id}/resume`, {
                        resume: true,
                      }),
                    );
                    setRun(next);
                    setStatus(next.status);
                    setRunId(next.run_id);
                    setStreamEpoch((n) => n + 1);
                  } catch (e) {
                    setError((e as Error).message);
                  }
                }}
              >
                继续当前任务
              </Button>
            </div>
          )}
          <div ref={scroll} />
        </div>
        <div className="composer-area">
          {connection && <Notice>{connection}</Notice>}
          {error && (
            <Notice error>
              {error}
              {pending.current && !active && (
                <button className="text-button" onClick={() => send(true)}>
                  <RefreshCw size={13} />
                  重试原请求
                </button>
              )}
            </Notice>
          )}
          {history.error && (
            <Notice error>会话消息无法读取，请刷新或选择其他会话。</Notice>
          )}
          <div className="row between">
            <AssistantAvatar
              status={status}
              focused={focused}
              working={working}
            />
            <span className="small muted">
              {quota.isError
                ? "额度暂时无法读取"
                : q
                  ? `今日剩余 ${q.remaining} / ${q.daily_limit} 次`
                  : "正在读取额度"}
            </span>
          </div>
          {cooldown && (
            <Notice>
              AI 将于 {new Date(q!.cooldown_until!).toLocaleString("zh-CN")}{" "}
              开放。你现在仍可阅读和留言。
            </Notice>
          )}
          {q?.remaining === 0 && (
            <Notice>
              今日额度已用完，下次重置：
              {new Date(q.next_reset_at).toLocaleString("zh-CN")}。
            </Notice>
          )}
          <form
            className="composer"
            onSubmit={(e) => {
              e.preventDefault();
              send();
            }}
          >
            <label className="sr-only" htmlFor="composer">
              向助手提问
            </label>
            <textarea
              id="composer"
              rows={2}
              maxLength={8000}
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                if (!busy) pending.current = null;
              }}
              onFocus={() => setFocused(true)}
              onBlur={() => setFocused(false)}
              onKeyDown={(e) => {
                if (
                  e.key === "Enter" &&
                  !e.shiftKey &&
                  !e.nativeEvent.isComposing
                ) {
                  e.preventDefault();
                  send();
                }
              }}
              placeholder={
                owner ? "把想处理的事交给助手…" : "此刻，你好奇什么？"
              }
            />
            <div className="row between">
              <div className="row">
                <span className="small muted">
                  <BookOpen size={14} />
                  站内资料
                </span>
                {owner && (
                  <label className="search-toggle">
                    <input
                      type="checkbox"
                      checked={searchMode === "web"}
                      onChange={(e) =>
                        setSearchMode(e.target.checked ? "web" : "site")
                      }
                    />
                    <Globe size={14} />
                    联网搜索
                  </label>
                )}
              </div>
              {active ? (
                <button
                  type="button"
                  className="send-button stop"
                  disabled={status === "cancelling"}
                  aria-label="停止回答"
                  onClick={async () => {
                    try {
                      await mutate(`/runs/${runId}/cancel`);
                      setStatus("cancelling");
                    } catch (e) {
                      setError((e as Error).message);
                    }
                  }}
                >
                  <Square size={16} />
                </button>
              ) : (
                <button
                  type="submit"
                  className="send-button"
                  disabled={!draft.trim() || busy || quotaBlocked}
                  aria-label="发送问题"
                >
                  <ArrowUp size={21} />
                </button>
              )}
            </div>
          </form>
          <p className="composer-footnote">
            {isDemo
              ? "这是模拟回答，可体验交互；不会调用模型或联网服务。"
              : "AI 可能出错，请通过引用核对内容。Enter 发送，Shift + Enter 换行。"}
          </p>
        </div>
      </section>
      {citations && (
        <Modal title="回答的来源" onClose={() => setCitations(null)}>
          <div className="citation-list">
            {citations.map((c, i) => {
              const url = c.url?.startsWith("/notes/")
                ? c.url
                : safeExternal(c.url);
              return (
                <div className="inset" key={c.id || i}>
                  <span className="eyebrow">SOURCE / {i + 1}</span>
                  <h3>{c.title}</h3>
                  {c.page && <p className="small muted">第 {c.page} 页</p>}
                  {c.section && <p className="small muted">{c.section}</p>}
                  <p>{c.excerpt}</p>
                  {url && (
                    <a
                      href={url}
                      target={url.startsWith("/") ? undefined : "_blank"}
                      rel="noopener noreferrer"
                    >
                      打开来源 ↗
                    </a>
                  )}
                </div>
              );
            })}
          </div>
        </Modal>
      )}
      {rename && (
        <Modal title="为这段对话起个名字" onClose={() => setRename(null)}>
          <form
            className="form-stack"
            onSubmit={async (e) => {
              e.preventDefault();
              try {
                await mutate(
                  `/conversations/${rename.id}`,
                  { title: renameTitle, expected_version: rename.version },
                  "PATCH",
                );
                setRename(null);
                invalidate();
              } catch (e) {
                setError((e as Error).message);
              }
            }}
          >
            <label>
              会话名称
              <input
                required
                maxLength={100}
                value={renameTitle}
                onChange={(e) => setRenameTitle(e.target.value)}
              />
            </label>
            <Button className="primary">保存名称</Button>
          </form>
        </Modal>
      )}
      {deleteTarget && (
        <Modal title="删除这段对话？" onClose={() => setDeleteTarget(null)}>
          <p>这段对话将从你的会话列表中移除。</p>
          <Button
            className="primary"
            onClick={async () => {
              try {
                await mutate(
                  `/conversations/${deleteTarget.id}`,
                  undefined,
                  "DELETE",
                );
                if (deleteTarget.id === conversationRef.current) {
                  cleanup.current?.();
                  setMessages([]);
                  setStatus(undefined);
                  setRunId(null);
                  conversationRef.current = null;
                  router.push(base);
                }
                setDeleteTarget(null);
                invalidate();
              } catch (e) {
                setError((e as Error).message);
              }
            }}
          >
            确认删除
          </Button>
        </Modal>
      )}
    </div>
  );
}

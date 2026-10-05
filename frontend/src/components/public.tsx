"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  ArrowUpRight,
  Search,
  MessageSquare,
  CornerDownRight,
  Flag,
  Send,
} from "lucide-react";
import { useState } from "react";
import { api, isDemo, mutate, newKey } from "@/lib/api";
import type { Comment, Page, PublicResource } from "@/lib/contracts";
import { formatDate, safeExternal } from "@/lib/safety";
import { useSession } from "./providers";
import { Button, Empty, Modal, Notice } from "./ui";
export function Cover({ variant = 0 }: { variant?: number }) {
  return (
    <div className={`note-cover cover-${variant % 3}`} aria-hidden="true">
      <span className="cover-orbit" />
      <span className="cover-window" />
      <span className="cover-book book-one" />
      <span className="cover-book book-two" />
      <span className="cover-leaf leaf-one" />
      <span className="cover-leaf leaf-two" />
      <span className="cover-caption">
        {
          ["MOMENTS & NOTES", "IDEAS IN PROGRESS", "BETWEEN THE PAGES"][
            variant % 3
          ]
        }
      </span>
    </div>
  );
}
export function NoteList({
  initial,
  compact = false,
}: {
  initial: Page<PublicResource>;
  compact?: boolean;
}) {
  const query = useQuery({
      queryKey: ["public", "notes"],
      queryFn: () => api<Page<PublicResource>>("/public/notes"),
      initialData: initial,
      staleTime: 0,
    }),
    [search, setSearch] = useState(""),
    [tag, setTag] = useState("全部"),
    [extra, setExtra] = useState<PublicResource[]>([]),
    [cursor, setCursor] = useState<string | null | undefined>(undefined),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const all = [...query.data.items, ...extra],
    tags = ["全部", ...new Set(all.flatMap((n) => n.tags))];
  const items = all.filter(
    (n) =>
      (tag === "全部" || n.tags.includes(tag)) &&
      `${n.title}${n.body}`.includes(search),
  );
  const next = cursor === undefined ? query.data.next_cursor : cursor;
  return (
    <>
      {!compact && (
        <div className="filter-bar">
          <div className="tabs" role="group" aria-label="手记分类">
            {tags.map((t) => (
              <button
                key={t}
                className={tag === t ? "active" : ""}
                onClick={() => setTag(t)}
              >
                {t}
              </button>
            ))}
          </div>
          <label className="search-box">
            <Search size={17} />
            <input
              aria-label="搜索手记"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="找一段感兴趣的文字"
            />
          </label>
        </div>
      )}
      {query.error && <Notice error>手记暂时无法刷新，请稍后再试。</Notice>}
      {items.length ? (
        <div className={compact ? "notes-compact" : "notes-grid"}>
          {(compact ? items.slice(0, 2) : items).map((note, index) => (
            <Link
              href={`/notes/${note.slug}`}
              key={note.id}
              className="note-card"
            >
              <Cover variant={index} />
              <div className="note-card-copy">
                <p className="eyebrow">{note.tags.join(" / ")}</p>
                <h3>{note.title}</h3>
                {!compact && (
                  <p className="muted">
                    {note.body?.replace(/[#*>\n]/g, "").slice(0, 58)}…
                  </p>
                )}
                <div className="row between small">
                  <time>{formatDate(note.published_at)}</time>
                  <ArrowRight size={18} />
                </div>
              </div>
            </Link>
          ))}
        </div>
      ) : (
        <Empty title="没有找到这段文字">换个关键词，或看看其他分类。</Empty>
      )}
      {!compact && next && (
        <Button
          busy={busy}
          onClick={async () => {
            setBusy(true);
            try {
              const page = await api<Page<PublicResource>>(
                `/public/notes?cursor=${encodeURIComponent(next)}`,
              );
              setExtra([...extra, ...page.items]);
              setCursor(page.next_cursor);
            } catch (e) {
              setError((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          继续翻页
        </Button>
      )}
      {error && <Notice error>{error}</Notice>}
    </>
  );
}
export function BookmarkList({
  initial,
  compact = false,
}: {
  initial: Page<PublicResource>;
  compact?: boolean;
}) {
  const query = useQuery({
      queryKey: ["public", "bookmarks"],
      queryFn: () => api<Page<PublicResource>>("/public/bookmarks"),
      initialData: initial,
      staleTime: 0,
    }),
    [tag, setTag] = useState("全部"),
    [search, setSearch] = useState(""),
    [extra, setExtra] = useState<PublicResource[]>([]),
    [cursor, setCursor] = useState<string | null | undefined>(),
    [error, setError] = useState("");
  const all = [...query.data.items, ...extra],
    tags = ["全部", ...new Set(all.flatMap((b) => b.tags))],
    items = all.filter(
      (b) =>
        (tag === "全部" || b.tags.includes(tag)) &&
        `${b.title}${b.note}${b.url}`.includes(search),
    );
  const next = cursor === undefined ? query.data.next_cursor : cursor;
  return (
    <>
      {!compact && (
        <div className="filter-bar">
          <div className="tabs">
            {tags.map((t) => (
              <button
                className={tag === t ? "active" : ""}
                key={t}
                onClick={() => setTag(t)}
              >
                {t}
              </button>
            ))}
          </div>
          <label className="search-box">
            <Search size={17} />
            <input
              aria-label="搜索收藏"
              placeholder="搜索标题、备注或网址"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
        </div>
      )}
      {query.error && <Notice error>收藏暂时无法刷新。</Notice>}
      <div className={compact ? "bookmark-compact" : "bookmark-grid"}>
        {(compact ? items.slice(0, 3) : items).map((item, index) => {
          const url = safeExternal(item.url);
          return (
            <a
              key={item.id}
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="bookmark-card"
            >
              <span className={`bookmark-symbol symbol-${index % 3}`}>
                {["↗", "⌘", "✳"][index % 3]}
              </span>
              <div>
                <span className="eyebrow">{item.tags.join(" / ")}</span>
                <h3>{item.title}</h3>
                {!compact && (
                  <>
                    <p className="muted">{item.note}</p>
                    <span className="small muted">
                      {url ? new URL(url).hostname : "未公开网址"}
                    </span>
                  </>
                )}
              </div>
              <ArrowUpRight size={19} />
            </a>
          );
        })}
      </div>
      {!items.length && <Empty title="暂时没有这一类收藏" />}
      {!compact && next && (
        <Button
          onClick={async () => {
            try {
              const page = await api<Page<PublicResource>>(
                `/public/bookmarks?cursor=${encodeURIComponent(next)}`,
              );
              setExtra([...extra, ...page.items]);
              setCursor(page.next_cursor);
            } catch (e) {
              setError((e as Error).message);
            }
          }}
        >
          查看更多
        </Button>
      )}
      {error && <Notice error>{error}</Notice>}
    </>
  );
}
export function Guestbook({
  initial,
  resourceId = null,
}: {
  initial: Page<Comment>;
  resourceId?: string | null;
}) {
  const session = useSession(),
    query = useQuery({
      queryKey: ["public", "comments", resourceId],
      queryFn: () =>
        api<Page<Comment>>(
          `/public/comments${resourceId ? `?resource_id=${resourceId}` : ""}`,
        ),
      initialData: initial,
      staleTime: 0,
    }),
    [body, setBody] = useState(""),
    [pending, setPending] = useState<Comment[]>([]),
    [reply, setReply] = useState<Comment | null>(null),
    [report, setReport] = useState<Comment | null>(null),
    [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [clientId, setClientId] = useState<string | null>(null);
  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!session.user) {
      session.openLogin();
      return;
    }
    if (!session.user.verified_at) {
      setError("请先验证邮箱，完成后即可留言。");
      return;
    }
    const key = clientId || newKey();
    setClientId(key);
    setBusy(true);
    setError("");
    try {
      const comment = await mutate<Comment>("/comments", {
        resource_id: resourceId,
        parent_id: reply?.id || null,
        body: body.trim(),
        client_id: key,
      });
      setPending([comment, ...pending]);
      setBody("");
      setReply(null);
      setClientId(null);
      session.toast(
        isDemo ? "留言已进入演示审核队列。" : "留言已提交，审核后公开。",
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="guestbook-layout">
      <div>
        <div className="section-label">
          <MessageSquare size={17} />
          <span>来过，就留下一点想法。</span>
        </div>
        {query.error && <Notice error>留言暂时无法加载。</Notice>}
        {[
          ...pending,
          ...query.data.items.filter(
            (c) => !pending.some((p) => p.id === c.id),
          ),
        ].map((c) => (
          <article className="comment-card" key={c.id}>
            <div className="row between">
              <div className="row">
                <span className="comment-avatar">
                  {c.author_display_name[0]}
                </span>
                <strong>{c.author_display_name}</strong>
                {c.status === "pending" && (
                  <span className="pill">待审核 · 仅本页可见</span>
                )}
              </div>
              <time className="small muted">{formatDate(c.created_at)}</time>
            </div>
            {c.parent_id && (
              <p className="small muted">
                <CornerDownRight size={12} /> 回复了一条留言
              </p>
            )}
            <p className="comment-body">{c.body}</p>
            <div className="row small">
              <button
                className="text-button"
                onClick={() => {
                  setReply(c);
                  document.getElementById("comment-input")?.focus();
                }}
              >
                回复
              </button>
              {c.status === "approved" && (
                <button
                  className="text-button muted"
                  onClick={() => {
                    if (!session.user) session.openLogin();
                    else setReport(c);
                  }}
                >
                  <Flag size={12} />
                  举报
                </button>
              )}
            </div>
          </article>
        ))}
        {!pending.length && !query.data.items.length && (
          <Empty title="第一句话，留给你" />
        )}
      </div>
      <form onSubmit={send} className="panel comment-form">
        <p className="eyebrow">LEAVE A LITTLE NOTE</p>
        <h2>写一封短笺</h2>
        <p className="muted small">分享一个想法，推荐一本书，或者说声你好。</p>
        {reply && (
          <div className="notice small">
            回复 {reply.author_display_name}
            <button
              type="button"
              className="text-button"
              onClick={() => setReply(null)}
            >
              取消
            </button>
          </div>
        )}
        <label className="sr-only" htmlFor="comment-input">
          留言内容
        </label>
        <textarea
          id="comment-input"
          required
          minLength={1}
          maxLength={2000}
          rows={6}
          placeholder="此刻，你想留下什么？"
          value={body}
          onChange={(e) => {
            setBody(e.target.value);
            setClientId(null);
          }}
        />
        <div className="row between small muted">
          <span>
            {session.user
              ? `以 ${session.user.display_name} 留言`
              : "登录后发送，草稿会保留"}
          </span>
          <span>{body.length}/2000</span>
        </div>
        {error && <Notice error>{error}</Notice>}
        <Button busy={busy} className="primary">
          {session.user ? "寄出短笺" : "登录并继续"}
          <Send size={15} />
        </Button>
        <p className="small muted">留言审核后公开，请勿填写个人敏感信息。</p>
      </form>
      {report && (
        <Modal title="举报留言" onClose={() => setReport(null)}>
          <form
            className="form-stack"
            onSubmit={async (e) => {
              e.preventDefault();
              try {
                await mutate("/reports", { comment_id: report.id, reason });
                setReport(null);
                setReason("");
                session.toast("举报已提交。");
              } catch (e) {
                setError((e as Error).message);
              }
            }}
          >
            <label>
              举报原因
              <textarea
                required
                maxLength={1000}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
            <Button className="primary">提交举报</Button>
          </form>
        </Modal>
      )}
    </div>
  );
}

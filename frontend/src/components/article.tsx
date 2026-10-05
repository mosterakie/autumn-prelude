"use client";
import Link from "next/link";
import { Sparkles } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { PublicResource } from "@/lib/contracts";
import { formatDate } from "@/lib/safety";
import { Empty, Markdown, Notice, Spinner } from "./ui";
export function PublicArticle({ initial }: { initial: PublicResource }) {
  const query = useQuery({
    queryKey: ["public", "note", initial.slug],
    queryFn: () =>
      api<PublicResource>(`/public/notes/${encodeURIComponent(initial.slug)}`),
    initialData: initial,
    staleTime: 0,
    refetchOnMount: "always",
  });
  if (query.error)
    return (
      <div className="panel">
        <Empty title="这一页暂时无法阅读">
          内容可能尚未公开，或已经被收回。
        </Empty>
        <Notice error>{query.error.message}</Notice>
      </div>
    );
  // Revalidate a restored route before redisplaying its old public projection.
  if (query.isFetching) return <Spinner />;
  const note = query.data;
  return (
    <article className="panel article">
      <div className="article-heading">
        <p className="eyebrow">{note.tags.join(" / ")}</p>
        <h1>{note.title}</h1>
        <p className="small muted">
          {formatDate(note.published_at)} · 公开版本 {note.publication_no}
        </p>
      </div>
      <Markdown body={note.body || ""} />
      <div className="article-bottom">
        <span className="muted small">读到这里，记下一点自己的想法。</span>
        {note.ai_enabled && (
          <Link href={`/chat?resource=${note.id}`} className="button">
            <Sparkles size={15} />
            围绕这篇聊聊
          </Link>
        )}
      </div>
    </article>
  );
}

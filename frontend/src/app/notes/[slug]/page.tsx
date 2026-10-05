import { notFound } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { getNote, publicData } from "@/lib/server-data";
import { PublicArticle } from "@/components/article";
import { Guestbook } from "@/components/public";
import type { Comment, Page } from "@/lib/contracts";
export const dynamic = "force-dynamic";
export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const note = await getNote((await params).slug);
  return { title: note?.title || "文章未找到" };
}
export default async function Note({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const note = await getNote((await params).slug);
  if (!note) notFound();
  const comments = await publicData<Page<Comment>>(
    `/comments?resource_id=${note.id}`,
    { items: [], next_cursor: null },
  );
  return (
    <>
      <Link className="back-link" href="/notes">
        <ArrowLeft size={16} />
        回到手记
      </Link>
      <PublicArticle initial={note} />
      <div className="article-comments">
        <Guestbook initial={comments} resourceId={note.id} />
      </div>
    </>
  );
}

import "server-only";
import { notes, bookmarks, comments } from "./seeds";
import type { Page, PublicResource, Comment } from "./contracts";
export async function publicData<T>(path: string, demoValue: T): Promise<T> {
  if (process.env.NEXT_PUBLIC_API_MODE !== "api") return demoValue;
  const response = await fetch(
    `${process.env.FASTAPI_ORIGIN || "http://127.0.0.1:8000"}/api/public${path}`,
    { cache: "no-store" },
  );
  if (!response.ok) throw new Error("公开内容暂时无法加载，请稍后重试。");
  return (await response.json()).data;
}
export const getNotes = () =>
  publicData<Page<PublicResource>>("/notes", {
    items: notes,
    next_cursor: null,
  });
export const getBookmarks = () =>
  publicData<Page<PublicResource>>("/bookmarks", {
    items: bookmarks,
    next_cursor: null,
  });
export const getComments = () =>
  publicData<Page<Comment>>("/comments", {
    items: comments,
    next_cursor: null,
  });
export async function getNote(slug: string) {
  if (process.env.NEXT_PUBLIC_API_MODE !== "api")
    return notes.find((n) => n.slug === slug) || null;
  const response = await fetch(
    `${process.env.FASTAPI_ORIGIN || "http://127.0.0.1:8000"}/api/public/notes/${encodeURIComponent(slug)}`,
    { cache: "no-store" },
  );
  if (response.status === 404) return null;
  if (!response.ok) throw new Error("文章暂时无法加载。");
  return (await response.json()).data as PublicResource;
}

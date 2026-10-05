import type { Message, Run } from "./contracts";
export function normalizeSnapshot(
  value: Partial<Message> & { message_id?: string },
): Message {
  const id = value.id || value.message_id;
  if (
    !id ||
    typeof value.body !== "string" ||
    !Number.isInteger(value.content_version)
  )
    throw new Error("Invalid message snapshot");
  return {
    id,
    body: value.body,
    role: value.role || "assistant",
    content_version: value.content_version!,
    status: value.status || "composing",
    created_at: value.created_at || "",
    citations: value.citations || [],
  };
}
export function normalizeRun(
  value: Run & { id?: string; current_message?: Message | null },
): Run {
  return {
    ...value,
    run_id: value.run_id || value.id!,
    message: value.message || value.current_message || undefined,
  };
}
export function safeReturnPath(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//"))
    return "/account";
  try {
    const decoded = decodeURIComponent(value);
    if (
      /[\\\u0000-\u0020]/.test(decoded) ||
      decoded.startsWith("//") ||
      decoded.includes("://")
    )
      return "/account";
    return value;
  } catch {
    return "/account";
  }
}
export function safeExternal(value?: string): string | undefined {
  try {
    const url = new URL(value || "");
    return ["http:", "https:"].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}
export function applySnapshot(
  messages: Message[],
  snapshot: Message,
): Message[] {
  const old = messages.find((m) => m.id === snapshot.id);
  if (
    old &&
    (old.status === "hidden" || old.content_version >= snapshot.content_version)
  )
    return messages;
  return old
    ? messages.map((m) => (m.id === snapshot.id ? snapshot : m))
    : [...messages, snapshot];
}
export function hideSources(messages: Message[], ids: string[]): Message[] {
  return messages.map((m) =>
    ids.includes(m.id)
      ? { ...m, body: "", citations: [], status: "hidden" }
      : m,
  );
}
export function validateFile(
  file: Pick<File, "name" | "size">,
  maxBytes = 20 * 1024 * 1024,
): string | null {
  if (!/\.(pdf|docx)$/i.test(file.name))
    return "首版支持文本型 PDF 和 DOCX，不支持 DOC。";
  if (file.size > maxBytes) return "文件超过 20 MiB，请拆分后上传。";
  if (!file.size) return "文件为空，请重新选择。";
  return null;
}
export function formatDate(date?: string | null) {
  return date
    ? new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        timeZone: "Asia/Shanghai",
      }).format(new Date(date))
    : "—";
}

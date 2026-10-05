// Isolated, volatile preview adapter. This is never used when API_MODE=api.
// It does not authenticate real accounts or call model/search providers.
import { ApiError, type RequestOptions } from "./api";
import type {
  Action,
  Ask,
  Audit,
  Comment,
  Conversation,
  Identity,
  Job,
  Limits,
  Memory,
  Message,
  Quota,
  Resource,
  Run,
  User,
} from "./contracts";
import { bookmarks, comments as initialComments, notes } from "./seeds";
import { safeExternal, validateFile } from "./safety";
const now = () => new Date().toISOString(),
  id = () => crypto.randomUUID();
let user: User | null = null,
  used = 0;
let resources: Resource[] = [],
  comments = structuredClone(initialComments),
  conversations: Conversation[] = [],
  messages: Record<string, Message[]> = {},
  runs: Record<string, Run> = {},
  jobs: Job[] = [],
  actions: Action[] = [],
  memories: Memory[] = [],
  audit: Audit[] = [];
let limits: Limits = {
  version: 1,
  values: {
    daily_limit: 10,
    cooldown_hours: 24,
    per_minute: 3,
    concurrency: 1,
  },
};
let retention: { mode: string; version: number; ttl_days?: number } = {
  mode: "forever",
  version: 1,
};
const idempotency = new Map<string, { signature: string; result: unknown }>();
const timers = new Set<ReturnType<typeof setInterval>>();
export function resetDemo() {
  timers.forEach(clearInterval);
  timers.clear();
  user = null;
  used = 0;
  conversations = [];
  messages = {};
  runs = {};
  jobs = [];
  actions = [];
  memories = [];
  audit = [];
  comments = structuredClone(initialComments);
  idempotency.clear();
  limits = {
    version: 1,
    values: {
      daily_limit: 10,
      cooldown_hours: 24,
      per_minute: 3,
      concurrency: 1,
    },
  };
  retention = { mode: "forever", version: 1 };
  resources = [...notes, ...bookmarks].map((p) => ({
    id: p.id,
    kind: p.kind,
    slug: p.slug,
    version: 1,
    acl_version: 1,
    current_revision: {
      id: id(),
      revision_no: 1,
      title: p.title,
      body_text: p.body,
      url: p.url,
      tags: p.tags,
      private_note: "这是私人笔记示例，默认不公开。",
    },
    publication: structuredClone(p),
  }));
}
function requireUser() {
  if (!user) throw new ApiError("AUTH_REQUIRED", "请先登录。", 401);
  return user;
}
function requireOwner() {
  const u = requireUser();
  if (u.role !== "owner")
    throw new ApiError("FORBIDDEN", "此区域仅供站长使用。", 403);
  if (!u.step_up_expires_at || Date.parse(u.step_up_expires_at) <= Date.now())
    throw new ApiError("STEP_UP_REQUIRED", "请完成站长额外验证。", 403);
}
function quota(): Quota {
  const end = new Date();
  end.setUTCHours(16, 0, 0, 0);
  if (+end <= Date.now()) end.setUTCDate(end.getUTCDate() + 1);
  return {
    timezone: "Asia/Shanghai",
    daily_limit: limits.values.daily_limit,
    used,
    reserved: 0,
    remaining: Math.max(0, limits.values.daily_limit - used),
    cooldown_until: user?.ai_cooldown_until || null,
    next_reset_at: end.toISOString(),
    server_time: now(),
  };
}
function identity(): Identity {
  return {
    user: user && structuredClone(user),
    csrf_token: user ? "demo-only-csrf" : null,
    server_time: now(),
  };
}
function findResource(resourceId: string) {
  const r = resources.find((r) => r.id === resourceId);
  if (!r) throw new ApiError("NOT_FOUND", "找不到这条资料。", 404);
  return r;
}
function checkVersion(current: { version: number }, value: unknown) {
  if (current.version !== value)
    throw new ApiError(
      "VERSION_CONFLICT",
      "内容已发生变化，请刷新后再操作。",
      409,
    );
}
function makeAction(
  type: string,
  changes: Record<string, unknown>,
  target?: Resource,
): Action {
  const action: Action = {
    id: id(),
    type,
    target_id: target?.id,
    expected_version: target?.version ?? retention.version,
    expected_acl_version: target?.acl_version,
    parameters_hash: id(),
    summary:
      type === "publication"
        ? `公开「${target?.current_revision.title}」当前版本`
        : "修改内容保存策略",
    changes,
    impact:
      type === "publication"
        ? "选中的字段将对所有访客可见，可被允许的站内问答引用。"
        : "影响后续数据保留；已有数据仅在明确勾选后处理。",
    requires_confirmation: true,
    status: "awaiting_confirmation",
    expires_at: new Date(Date.now() + 15 * 60000).toISOString(),
    can_undo: false,
  };
  actions.push(action);
  return structuredClone(action);
}
export type DemoEvent = {
  name: string;
  data: Record<string, unknown>;
  sequence: string;
};
export function subscribeDemo(
  runId: string,
  onEvent: (event: DemoEvent) => void,
): () => void {
  const run = runs[runId];
  if (!run) return () => {};
  let sequence = 0;
  const emit = (name: string, data: Record<string, unknown>) =>
    onEvent({ name, data, sequence: String(++sequence) });
  const input =
    messages[run.conversation_id]?.filter((m) => m.role === "user").at(-1)
      ?.body || "";
  const text = `这是界面演示回答，尚未连接 DeepSeek。\n\n你问的是：**${input.replace(/[<>]/g, "").slice(0, 120)}**\n\n秋序把手记、收藏与 AI 对话放在同一个小站里。公开问答可以围绕公开手记给出来源；站长的私人助手可以进一步整理自己的资料。\n\n你可以打开下方来源，看看「让好奇心，有处安放」这篇示例手记。实际回答将在后端接入后生成。`;
  const answer: Message = run.message || {
    id: id(),
    role: "assistant",
    body: "",
    content_version: 0,
    status: "composing",
    citations: [],
    created_at: now(),
  };
  run.message = answer;
  let cursor = answer.body.length,
    announced = false;
  const timer = setInterval(() => {
    if (!user) {
      clearInterval(timer);
      timers.delete(timer);
      return;
    }
    if (run.status === "cancelling") {
      run.status = "cancelled";
      answer.status = "interrupted";
      emit("message.snapshot", {
        ...answer,
        content_version: ++answer.content_version,
      });
      emit("done", { run_id: runId, status: run.status });
      clearInterval(timer);
      timers.delete(timer);
      return;
    }
    if (!announced) {
      run.status = "running";
      emit("run.status", { run_id: runId, status: "running" });
      emit("tool.started", {
        display_name: "查阅公开手记（演示）",
        summary: "模拟寻找相关来源",
      });
      announced = true;
    }
    cursor = Math.min(text.length, cursor + 22);
    answer.body = text.slice(0, cursor);
    answer.content_version++;
    emit("message.snapshot", { ...answer });
    if (cursor === text.length) {
      answer.status = "complete";
      answer.content_version++;
      answer.citations = [
        {
          id: notes[0].publication_id,
          publication_id: notes[0].publication_id,
          title: notes[0].title,
          url: `/notes/${notes[0].slug}`,
          excerpt: "让零散的好奇慢慢串起来，成为可以回看的路径。",
        },
      ];
      messages[run.conversation_id] = [
        ...messages[run.conversation_id].filter((m) => m.id !== answer.id),
        structuredClone(answer),
      ];
      run.status = "succeeded";
      emit("message.snapshot", { ...answer });
      emit("tool.finished", { result_summary: "找到 1 条示例来源" });
      emit("done", { run_id: runId, status: "succeeded" });
      clearInterval(timer);
      timers.delete(timer);
    }
  }, 95);
  timers.add(timer);
  return () => {
    clearInterval(timer);
    timers.delete(timer);
  };
}
export async function demoRequest<T>(
  path: string,
  options: RequestOptions,
): Promise<T> {
  await new Promise((resolve) => setTimeout(resolve, 130));
  if (!resources.length) resetDemo();
  const method = options.method || "GET",
    raw = options.body;
  const body = (raw instanceof FormData ? {} : raw || {}) as Record<
    string,
    any
  >;
  const pathname = path.split("?")[0],
    parts = pathname.split("/").filter(Boolean),
    key = options.key
      ? `${user?.id || "guest"}:${method}:${pathname}:${options.key}`
      : null;
  const signature = JSON.stringify(body);
  if (key && idempotency.has(key)) {
    if (
      ["resources", "knowledge", "actions", "settings", "memories"].includes(
        parts[0],
      )
    )
      requireOwner();
    if (["ask", "conversations", "runs"].includes(parts[0])) requireUser();
    const previous = idempotency.get(key)!;
    if (previous.signature !== signature)
      throw new ApiError(
        "IDEMPOTENCY_CONFLICT",
        "原请求键不能用于不同内容。",
        409,
      );
    return structuredClone(previous.result) as T;
  }
  let result: unknown;
  if (pathname === "/auth/me") result = identity();
  else if (pathname === "/auth/login") {
    resetDemo();
    const owner = String(body.email).startsWith("owner@");
    user = {
      id: id(),
      display_name: owner ? "站长（演示）" : "秋日访客（演示）",
      email: body.email,
      role: owner ? "owner" : "member",
      status: String(body.email).startsWith("unverified@")
        ? "pending_verification"
        : "active",
      verified_at: String(body.email).startsWith("unverified@") ? null : now(),
      ai_cooldown_until: String(body.email).startsWith("cooldown@")
        ? new Date(Date.now() + 24 * 3600000).toISOString()
        : null,
      step_up_expires_at: null,
      capabilities: owner ? ["owner", "public_chat"] : ["public_chat"],
    };
    if (String(body.email).startsWith("limited@"))
      used = limits.values.daily_limit;
    result = identity();
  } else if (pathname === "/auth/logout") {
    resetDemo();
    result = undefined;
  } else if (pathname === "/auth/step-up") {
    if (requireUser().role !== "owner")
      throw new ApiError("FORBIDDEN", "仅站长可额外验证。", 403);
    if (body.totp_code !== "123456")
      throw new ApiError("INVALID_TOTP", "演示验证码为 123456。", 422);
    user!.step_up_expires_at = new Date(Date.now() + 15 * 60000).toISOString();
    result = identity();
  } else if (pathname.startsWith("/auth/")) result = { accepted: true };
  else if (pathname === "/me/quota") {
    requireUser();
    result = quota();
  } else if (pathname === "/public/notes")
    result = {
      items: resources.flatMap((r) =>
        r.kind === "article" && r.publication ? [r.publication] : [],
      ),
      next_cursor: null,
    };
  else if (parts[0] === "public" && parts[1] === "notes" && parts[2]) {
    const publication = resources.find((r) => r.slug === parts[2])?.publication;
    if (!publication)
      throw new ApiError("NOT_FOUND", "这篇手记尚未公开或已收回。", 404);
    result = publication;
  } else if (pathname === "/public/bookmarks")
    result = {
      items: resources.flatMap((r) =>
        r.kind === "bookmark" && r.publication ? [r.publication] : [],
      ),
      next_cursor: null,
    };
  else if (pathname === "/public/comments")
    result = {
      items: comments.filter(
        (c) =>
          c.status === "approved" &&
          c.resource_id ===
            (new URLSearchParams(path.split("?")[1]).get("resource_id") ||
              null),
      ),
      next_cursor: null,
    };
  else if (pathname === "/comments" && method === "POST") {
    const u = requireUser();
    if (!u.verified_at)
      throw new ApiError("EMAIL_UNVERIFIED", "请先验证邮箱。", 403);
    const existing = comments.find((c) => c.id === body.client_id);
    if (existing) result = existing;
    else {
      const c: Comment = {
        id: body.client_id,
        resource_id: body.resource_id || null,
        parent_id: body.parent_id || null,
        author_display_name: u.display_name,
        body: body.body,
        status: "pending",
        created_at: now(),
        version: 1,
      };
      comments.unshift(c);
      result = c;
    }
  } else if (pathname === "/reports" && method === "POST") {
    requireUser();
    result = { id: id(), status: "pending" };
  } else if (parts[0] === "conversations") {
    requireUser();
    if (
      body.mode === "owner" ||
      new URLSearchParams(path.split("?")[1]).get("mode") === "owner" ||
      conversations.find((c) => c.id === parts[1])?.mode === "owner"
    )
      requireOwner();
    if (parts.length === 1 && method === "GET")
      result = {
        items: conversations.filter(
          (c) =>
            c.mode ===
            (new URLSearchParams(path.split("?")[1]).get("mode") || "public"),
        ),
        next_cursor: null,
      };
    else if (parts.length === 1) {
      const c: Conversation = {
        id: id(),
        mode: body.mode,
        title: body.title || "新的对话",
        version: 1,
        created_at: now(),
      };
      conversations.unshift(c);
      messages[c.id] = [];
      result = c;
    } else {
      const c = conversations.find((c) => c.id === parts[1]);
      if (!c) throw new ApiError("NOT_FOUND", "会话不存在。", 404);
      if (parts[2] === "messages")
        result = { items: messages[c.id], next_cursor: null };
      else if (method === "PATCH") {
        checkVersion(c, body.expected_version);
        c.title = body.title;
        c.version++;
        result = c;
      } else if (method === "DELETE") {
        conversations = conversations.filter((v) => v.id !== c.id);
        delete messages[c.id];
        result = undefined;
      } else result = c;
    }
  } else if (pathname === "/ask") {
    const u = requireUser(),
      ask = body as unknown as Ask,
      c = conversations.find((c) => c.id === ask.conversation_id);
    if (!c) throw new ApiError("NOT_FOUND", "会话不存在。", 404);
    if (c.mode === "owner" || ask.search_mode === "web") requireOwner();
    if (!u.verified_at)
      throw new ApiError("EMAIL_UNVERIFIED", "请先验证邮箱。", 403);
    if (u.ai_cooldown_until && Date.parse(u.ai_cooldown_until) > Date.now())
      throw new ApiError("AI_COOLDOWN", "仍在注册冷却期。", 429);
    if (!quota().remaining)
      throw new ApiError("QUOTA_EXCEEDED", "今日额度已用完。", 429);
    if (
      Object.values(runs).some((r) =>
        ["queued", "running", "cancelling"].includes(r.status),
      )
    )
      throw new ApiError("CONCURRENCY_LIMIT", "请等当前回答结束。", 429);
    used++;
    messages[c.id].push({
      id: ask.client_message_id,
      body: ask.message,
      role: "user",
      status: "complete",
      content_version: 1,
      created_at: now(),
      citations: [],
    });
    c.title = c.title === "新的对话" ? ask.message.slice(0, 24) : c.title;
    const run: Run = { run_id: id(), conversation_id: c.id, status: "queued" };
    runs[run.run_id] = run;
    c.active_run_id = run.run_id;
    result = {
      ...run,
      events_url: `/api/runs/${run.run_id}/events`,
      quota: quota(),
    };
  } else if (parts[0] === "runs") {
    requireUser();
    const r = runs[parts[1]];
    if (!r) throw new ApiError("NOT_FOUND", "任务不存在。", 404);
    if (conversations.find((c) => c.id === r.conversation_id)?.mode === "owner")
      requireOwner();
    if (parts[2] === "cancel") r.status = "cancelling";
    if (parts[2] === "resume") r.status = "running";
    result = r;
  } else {
    requireOwner();
    if (parts[0] === "resources") {
      if (parts.length === 1 && method === "GET")
        result = { items: resources, next_cursor: null };
      else if (parts.length === 1) {
        const r: Resource = {
          id: id(),
          kind: body.kind,
          slug: id(),
          version: 1,
          acl_version: 1,
          publication: null,
          current_revision: {
            id: id(),
            revision_no: 1,
            title: body.title,
            body_text: body.body_text,
            url: body.url,
            private_note: body.private_note,
            tags: body.tags || [],
          },
        };
        resources.unshift(r);
        result = r;
      } else {
        const r = findResource(parts[1]);
        if (parts[2] === "publication" && parts[3] === "preview") {
          checkVersion(r, body.expected_version);
          result = makeAction("publication", body, r);
        } else if (parts[2] === "publication" && parts[3] === "revoke") {
          checkVersion(r, body.expected_version);
          r.publication = null;
          r.version++;
          r.acl_version++;
          result = r;
        } else if (parts[2] === "versions")
          result = { items: [r.current_revision], next_cursor: null };
        else if (parts[2] === "refresh") {
          const j: Job = {
            id: id(),
            resource_id: r.id,
            status: "queued",
            phase: "fetching",
            progress: null,
            can_retry: false,
          };
          jobs.unshift(j);
          result = { resource: r, job: j };
        } else if (method === "PATCH") {
          checkVersion(r, body.expected_version);
          r.version++;
          r.current_revision = {
            ...r.current_revision,
            ...(body.changes || body),
            id: id(),
            revision_no: r.current_revision.revision_no + 1,
          };
          result = r;
        } else if (method === "DELETE") {
          checkVersion(r, body.expected_version);
          resources = resources.filter((item) => item.id !== r.id);
          result = undefined;
        } else result = r;
      }
    } else if (parts[0] === "knowledge") {
      const file = raw instanceof FormData ? (raw.get("file") as File) : null;
      if (file) {
        const error = validateFile(file);
        if (error) throw new ApiError("UNSUPPORTED_FILE_TYPE", error, 415);
      } else if (!safeExternal(body.url))
        throw new ApiError(
          "INVALID_URL",
          "请使用完整的 HTTP 或 HTTPS 网页链接。",
          422,
        );
      const r: Resource = {
        id: id(),
        kind: file ? "document" : "bookmark",
        slug: id(),
        version: 1,
        acl_version: 1,
        publication: null,
        current_revision: {
          id: id(),
          title: file?.name || body.title || body.url,
          url: body.url,
          tags: body.tags || [],
          revision_no: 1,
        },
      };
      resources.unshift(r);
      const j: Job = {
        id: id(),
        resource_id: r.id,
        status: body.mode === "bookmark_only" ? "succeeded" : "queued",
        phase: file ? "parsing" : "fetching",
        progress: null,
        can_retry: false,
      };
      jobs.unshift(j);
      r.processing_jobs = [j];
      result = { resource: r, job: j };
    } else if (parts[0] === "jobs") {
      const j = jobs.find((j) => j.id === parts[1]);
      if (!j) throw new ApiError("NOT_FOUND", "任务不存在。", 404);
      if (parts[2] === "cancel") {
        j.status = "cancelled";
        j.can_retry = true;
      } else if (parts[2] === "retry") {
        j.status = "queued";
        j.can_retry = false;
      } else if (["queued", "running"].includes(j.status)) {
        if (j.status === "queued") j.status = "running";
        else if (j.phase === "fetching") j.phase = "parsing";
        else if (j.phase === "parsing") j.phase = "embedding";
        else j.status = "succeeded";
      }
      result = j;
    } else if (parts[0] === "actions") {
      const a = actions.find((a) => a.id === parts[1]);
      if (!a) throw new ApiError("NOT_FOUND", "动作不存在。", 404);
      if (parts[2] === "cancel") a.status = "cancelled";
      else if (parts[2] === "execute") {
        if (a.parameters_hash !== body.parameters_hash)
          throw new ApiError("VERSION_CONFLICT", "预览参数发生变化。", 409);
        if (Date.parse(a.expires_at) <= Date.now() || a.status === "cancelled")
          throw new ApiError("ACTION_EXPIRED", "预览已过期，请重新生成。", 409);
        if (a.status !== "succeeded") {
          if (a.type === "publication") {
            const r = findResource(a.target_id!);
            checkVersion(r, a.expected_version);
            if (r.acl_version !== a.expected_acl_version)
              throw new ApiError(
                "VERSION_CONFLICT",
                "公开权限版本已变化。",
                409,
              );
            const rev = r.current_revision,
              fields = a.changes.public_fields as string[];
            r.publication = {
              id: r.id,
              kind: r.kind,
              slug: r.slug,
              title: fields.includes("title") ? rev.title : "未命名公开内容",
              body: fields.includes("body_text") ? rev.body_text : undefined,
              note: fields.includes("private_note")
                ? rev.private_note
                : undefined,
              url: fields.includes("url") ? rev.url : undefined,
              tags: fields.includes("tags") ? rev.tags : [],
              publication_id: id(),
              publication_no: (r.publication?.publication_no || 0) + 1,
              ai_enabled: !!a.changes.ai_enabled,
              raw_download_enabled: !!a.changes.raw_download_enabled,
              published_at: now(),
            };
            r.version++;
            r.acl_version++;
          } else
            retention = {
              mode: String(a.changes.mode),
              ttl_days: Number(a.changes.ttl_days),
              version: retention.version + 1,
            };
          a.status = "succeeded";
          audit.unshift({ id: id(), summary: a.summary, created_at: now() });
        }
      }
      result = a;
    } else if (pathname === "/settings/ai-limits") {
      if (method === "PATCH") {
        checkVersion(limits, body.expected_version);
        limits = { version: limits.version + 1, values: body.values };
      }
      result = limits;
    } else if (pathname === "/settings/retention") result = retention;
    else if (pathname === "/settings/retention/preview")
      result = makeAction("retention", body);
    else if (parts[0] === "memories") {
      if (method === "GET") result = { items: memories, next_cursor: null };
      else if (method === "POST") {
        const m = { id: id(), content: body.content, version: 1 };
        memories.push(m);
        result = m;
      } else if (method === "DELETE") {
        memories = memories.filter((m) => m.id !== parts[1]);
        result = undefined;
      } else {
        const m = memories.find((m) => m.id === parts[1]);
        if (!m) throw new ApiError("NOT_FOUND", "记忆不存在。", 404);
        checkVersion(m, body.expected_version);
        m.content = body.content;
        m.version++;
        result = m;
      }
    } else if (pathname === "/moderation/comments")
      result = {
        items: comments.filter((c) => c.status === "pending"),
        next_cursor: null,
      };
    else if (parts[0] === "moderation" && parts[1] === "comments") {
      const c = comments.find((c) => c.id === parts[2]);
      if (!c) throw new ApiError("NOT_FOUND", "留言不存在。", 404);
      checkVersion(c, body.expected_version);
      c.status = body.decision === "approve" ? "approved" : "rejected";
      c.version++;
      result = c;
    } else if (pathname === "/moderation/reports")
      result = { items: [], next_cursor: null };
    else if (pathname === "/audit-events")
      result = { items: audit, next_cursor: null };
    else throw new ApiError("DEMO_NOT_SUPPORTED", "此操作等待后端接入。", 501);
  }
  if (key) idempotency.set(key, { signature, result: structuredClone(result) });
  return structuredClone(result) as T;
}

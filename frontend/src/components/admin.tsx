"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  BookOpen,
  FileText,
  Link as LinkIcon,
  Plus,
  Upload,
  ExternalLink,
  ShieldCheck,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { useIdempotentRequest } from "@/lib/use-request";
import { api, isDemo, mutate, newKey } from "@/lib/api";
import type {
  Action,
  Audit,
  Comment,
  Job,
  Limits,
  Memory,
  Page,
  Report,
  Resource,
} from "@/lib/contracts";
import { formatDate, safeExternal, validateFile } from "@/lib/safety";
import { useSession } from "./providers";
import { ActionPreview } from "./actions";
import {
  Button,
  Empty,
  Markdown,
  Modal,
  Notice,
  PageHeading,
  Spinner,
} from "./ui";
const kindLabel = { article: "手记", bookmark: "收藏", document: "文档" };
export function ContentAdmin({
  initialKind,
}: {
  initialKind?: "article" | "bookmark";
}) {
  const request = useIdempotentRequest();
  const session = useSession(),
    client = useQueryClient(),
    key = ["resources", session.user!.id, "owner"],
    query = useQuery({
      queryKey: key,
      queryFn: () => api<Page<Resource>>("/resources"),
    });
  const [editing, setEditing] = useState<Resource | "new" | null>(
      initialKind ? "new" : null,
    ),
    [newKind, setNewKind] = useState<"article" | "bookmark">(
      initialKind || "article",
    ),
    [preview, setPreview] = useState<Resource | null>(null),
    [remove, setRemove] = useState<Resource | null>(null),
    [error, setError] = useState(""),
    [filter, setFilter] = useState("全部");
  const refresh = () => client.invalidateQueries({ queryKey: key });
  return (
    <>
      <PageHeading
        eyebrow="WORKSPACE / CONTENT"
        title="整理每一段思考"
        aside={
          <div className="row wrap">
            <Button
              className="primary"
              onClick={() => {
                setNewKind("article");
                setEditing("new");
              }}
            >
              <Plus size={16} /> 写手记
            </Button>
            <Button
              onClick={() => {
                setNewKind("bookmark");
                setEditing("new");
              }}
            >
              <LinkIcon size={16} /> 收藏网址
            </Button>
          </div>
        }
      >
        草稿可以慢慢写。公开哪个版本，由你决定。
      </PageHeading>
      <div className="tabs margin-bottom">
        {["全部", "手记", "收藏", "文档"].map((t) => (
          <button
            key={t}
            className={filter === t ? "active" : ""}
            onClick={() => setFilter(t)}
          >
            {t}
          </button>
        ))}
      </div>
      {error && <Notice error>{error}</Notice>}
      {query.isPending ? (
        <Spinner />
      ) : query.error ? (
        <Notice error>{query.error.message}</Notice>
      ) : !query.data.items.length ? (
        <Empty />
      ) : (
        <div className="resource-list">
          {query.data.items
            .filter((r) => filter === "全部" || kindLabel[r.kind] === filter)
            .map((r) => (
              <article className="panel resource-card" key={r.id}>
                <div className="row between">
                  <div className="row">
                    <span className="resource-icon">
                      {r.kind === "article" ? (
                        <FileText size={21} />
                      ) : r.kind === "bookmark" ? (
                        <LinkIcon size={21} />
                      ) : (
                        <BookOpen size={21} />
                      )}
                    </span>
                    <div>
                      <p className="eyebrow">
                        {kindLabel[r.kind]} / 私人版本{" "}
                        {r.current_revision.revision_no}
                      </p>
                      <h3>{r.current_revision.title}</h3>
                    </div>
                  </div>
                  <span className={`pill ${r.publication ? "green" : ""}`}>
                    {r.publication
                      ? `公开版本 ${r.publication.publication_no}`
                      : "未公开"}
                  </span>
                </div>
                <p className="small muted">
                  {r.current_revision.tags.join(" · ") || "暂无标签"} · 内容版本{" "}
                  {r.version} · 权限版本 {r.acl_version}
                </p>
                {r.publication &&
                  r.current_revision.title !== r.publication.title && (
                    <Notice>
                      当前草稿已修改，公开页面仍展示上一次发布的版本。
                    </Notice>
                  )}
                <div className="row wrap">
                  <Button className="compact" onClick={() => setEditing(r)}>
                    编辑草稿
                  </Button>
                  <Button className="compact" onClick={() => setPreview(r)}>
                    预览公开
                  </Button>
                  {r.publication && (
                    <Button
                      className="compact"
                      onClick={async () => {
                        try {
                          await request(
                            `/resources/${r.id}/publication/revoke`,
                            {
                              expected_version: r.version,
                              expected_acl_version: r.acl_version,
                            },
                          );
                          refresh();
                          client.invalidateQueries({ queryKey: ["public"] });
                          session.toast("公开状态已收回。");
                        } catch (e) {
                          setError((e as Error).message);
                        }
                      }}
                    >
                      收回公开
                    </Button>
                  )}
                  <button
                    className="icon-button"
                    aria-label={`删除${r.current_revision.title}`}
                    onClick={() => setRemove(r)}
                  >
                    <Trash2 size={16} />
                  </button>
                </div>
              </article>
            ))}
        </div>
      )}
      {editing && (
        <Modal
          title={
            editing === "new"
              ? newKind === "bookmark"
                ? "收藏网址"
                : "写一篇手记"
              : "编辑私人草稿"
          }
          onClose={() => setEditing(null)}
        >
          <ResourceEditor
            resource={editing === "new" ? null : editing}
            initialKind={newKind}
            onSaved={() => {
              refresh();
              setEditing(null);
            }}
          />
        </Modal>
      )}
      {preview && (
        <Modal title="公开当前版本" onClose={() => setPreview(null)}>
          <PublicationPreview
            resource={preview}
            onDone={() => {
              refresh();
              client.invalidateQueries({ queryKey: ["public"] });
            }}
          />
        </Modal>
      )}
      {remove && (
        <Modal title="删除这条资料？" onClose={() => setRemove(null)}>
          <p>
            「{remove.current_revision.title}
            」会从资料列表中移除。如有公开版本，将一并收回。
          </p>
          <Button
            className="primary"
            onClick={async () => {
              try {
                await request(
                  `/resources/${remove.id}`,
                  {
                    expected_version: remove.version,
                    expected_acl_version: remove.acl_version,
                  },
                  "DELETE",
                );
                setRemove(null);
                refresh();
                client.invalidateQueries({ queryKey: ["public"] });
              } catch (e) {
                setError((e as Error).message);
              }
            }}
          >
            确认删除
          </Button>
        </Modal>
      )}
    </>
  );
}
function ResourceEditor({
  resource,
  initialKind,
  onSaved,
}: {
  resource: Resource | null;
  initialKind: "article" | "bookmark";
  onSaved: () => void;
}) {
  const request = useIdempotentRequest();
  const [kind, setKind] = useState<Resource["kind"]>(
      resource?.kind || initialKind,
    ),
    [title, setTitle] = useState(resource?.current_revision.title || ""),
    [body, setBody] = useState(resource?.current_revision.body_text || ""),
    [url, setUrl] = useState(resource?.current_revision.url || ""),
    [note, setNote] = useState(resource?.current_revision.private_note || ""),
    [tags, setTags] = useState(
      resource?.current_revision.tags.join("，") || "",
    ),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  return (
    <form
      className="form-stack"
      onSubmit={async (e) => {
        e.preventDefault();
        if (kind === "bookmark" && !safeExternal(url)) {
          setError("请使用完整的 HTTP 或 HTTPS 网页链接。");
          return;
        }
        setBusy(true);
        setError("");
        try {
          const changes = {
            title,
            body_text: body,
            url,
            private_note: note,
            tags: tags
              .split(/[,，]/)
              .map((t) => t.trim())
              .filter(Boolean),
          };
          if (resource)
            await request(
              `/resources/${resource.id}`,
              { expected_version: resource.version, ...changes },
              "PATCH",
            );
          else await request("/resources", { kind, ...changes });
          onSaved();
        } catch (e) {
          setError((e as Error).message);
        } finally {
          setBusy(false);
        }
      }}
    >
      {!resource && (
        <label>
          内容类型
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as Resource["kind"])}
          >
            <option value="article">手记</option>
            <option value="bookmark">网址收藏</option>
          </select>
        </label>
      )}
      <label>
        标题
        <input
          required
          maxLength={200}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
      </label>
      {kind === "bookmark" ? (
        <label>
          网页链接
          <input
            type="url"
            required
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
        </label>
      ) : (
        <label>
          正文 · 支持 Markdown
          <textarea
            rows={10}
            value={body}
            onChange={(e) => setBody(e.target.value)}
          />
        </label>
      )}
      <label>
        标签
        <input
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          placeholder="阅读，随笔"
        />
      </label>
      <label>
        私人备注
        <textarea
          rows={3}
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      </label>
      <p className="small muted">
        此处保存私人草稿。公开页面会保留原发布版本，直到你明确发布新版本。
      </p>
      {error && <Notice error>{error}</Notice>}
      <Button busy={busy} className="primary">
        {kind === "bookmark" ? "保存私人收藏" : "保存私人草稿"}
      </Button>
    </form>
  );
}
function PublicationPreview({
  resource,
  onDone,
}: {
  resource: Resource;
  onDone: () => void;
}) {
  const request = useIdempotentRequest();
  const revision = resource.current_revision,
    defaults = [
      "title",
      "tags",
      ...(resource.kind === "article"
        ? ["body_text"]
        : resource.kind === "bookmark"
          ? ["url"]
          : []),
    ];
  const [fields, setFields] = useState(defaults),
    [ai, setAi] = useState(true),
    [download, setDownload] = useState(false),
    [action, setAction] = useState<Action | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  if (action)
    return (
      <ActionPreview
        action={action}
        onDone={onDone}
        onCancel={() => setAction(null)}
      />
    );
  return (
    <div className="form-stack">
      <Notice>只公开你选中的字段。私人备注和原文件下载默认关闭。</Notice>
      <div className="checkbox-grid">
        {[
          ["title", "标题"],
          ["body_text", "正文"],
          ["url", "网址"],
          ["tags", "标签"],
          ["private_note", "私人备注"],
        ]
          .filter(
            ([key]) =>
              key === "title" ||
              key === "tags" ||
              !!revision[key as keyof typeof revision],
          )
          .map(([key, label]) => (
            <label className="checkbox" key={key}>
              <input
                type="checkbox"
                checked={fields.includes(key)}
                onChange={(e) =>
                  setFields(
                    e.target.checked
                      ? [...fields, key]
                      : fields.filter((f) => f !== key),
                  )
                }
              />
              {label}
            </label>
          ))}
      </div>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={ai}
          onChange={(e) => setAi(e.target.checked)}
        />
        允许站内 AI 引用这个公开版本
      </label>
      {resource.kind === "document" && (
        <label className="checkbox">
          <input
            type="checkbox"
            checked={download}
            onChange={(e) => setDownload(e.target.checked)}
          />
          允许访客下载原文件
        </label>
      )}
      <div className="publication-render inset">
        <p className="eyebrow">PUBLIC PREVIEW / 拟公开内容</p>
        {fields.includes("title") && <h2>{revision.title}</h2>}
        {fields.includes("tags") && (
          <p className="small muted">{revision.tags.join(" / ")}</p>
        )}
        {fields.includes("body_text") && (
          <Markdown body={revision.body_text || ""} />
        )}{" "}
        {fields.includes("url") && <p>{revision.url}</p>}
        {fields.includes("private_note") && <p>{revision.private_note}</p>}
      </div>
      {error && <Notice error>{error}</Notice>}
      <Button
        className="primary"
        busy={busy}
        disabled={!fields.includes("title")}
        onClick={async () => {
          setBusy(true);
          try {
            setAction(
              await request<Action>(
                `/resources/${resource.id}/publication/preview`,
                {
                  revision_id: revision.id,
                  public_fields: fields,
                  ai_enabled: ai,
                  raw_download_enabled: download,
                  expected_version: resource.version,
                  expected_acl_version: resource.acl_version,
                },
              ),
            );
          } catch (e) {
            setError((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        生成操作预览
      </Button>
    </div>
  );
}
export function KnowledgeAdmin() {
  const request = useIdempotentRequest();
  const session = useSession(),
    [tab, setTab] = useState("link"),
    [url, setUrl] = useState(""),
    [title, setTitle] = useState(""),
    [mode, setMode] = useState("bookmark_and_knowledge"),
    [file, setFile] = useState<File | null>(null),
    [jobs, setJobs] = useState<Job[]>([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const resources = useQuery({
    queryKey: ["resources", session.user!.id, "owner"],
    queryFn: () => api<Page<Resource>>("/resources"),
  });
  const allJobs = Array.from(
    new Map(
      [
        ...jobs,
        ...(resources.data?.items.flatMap((r) => r.processing_jobs || []) ||
          []),
      ].map((j) => [j.id, j]),
    ).values(),
  );
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (tab === "file") {
      if (!file) {
        setError("请先选择一个文件。");
        return;
      }
      const invalid = validateFile(file);
      if (invalid) {
        setError(invalid);
        return;
      }
    } else if (!safeExternal(url)) {
      setError("请填写完整的 HTTP 或 HTTPS 链接。");
      return;
    }
    setBusy(true);
    try {
      let result: { resource: Resource; job: Job };
      if (tab === "file") {
        const form = new FormData();
        form.append("file", file!);
        form.append("title", title || file!.name);
        result = await request("/knowledge/files", form);
      } else
        result = await request("/knowledge/urls", {
          url,
          title,
          mode,
          tags: [],
        });
      setJobs([result.job, ...jobs]);
      setUrl("");
      setTitle("");
      setFile(null);
      resources.refetch();
      session.toast("已受理，请查看处理状态。资料尚未自动公开。");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <PageHeading eyebrow="WORKSPACE / KNOWLEDGE" title="给知识留一个位置">
        网页、PDF 和 Word 文档。先放进来，再慢慢建立联系。
      </PageHeading>
      <div className="knowledge-grid">
        <section className="panel knowledge-input">
          <div className="tabs">
            <button
              className={tab === "link" ? "active" : ""}
              onClick={() => setTab("link")}
            >
              <LinkIcon size={15} />
              粘贴网页
            </button>
            <button
              className={tab === "file" ? "active" : ""}
              onClick={() => setTab("file")}
            >
              <Upload size={15} />
              上传文件
            </button>
          </div>
          <form className="form-stack" onSubmit={submit}>
            {tab === "link" ? (
              <>
                <label>
                  网页地址
                  <input
                    type="url"
                    required
                    placeholder="https://…"
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                  />
                </label>
                <label>
                  处理方式
                  <select
                    value={mode}
                    onChange={(e) => setMode(e.target.value)}
                  >
                    <option value="bookmark_and_knowledge">
                      收藏并把正文放入知识库
                    </option>
                    <option value="knowledge_only">只把正文放入知识库</option>
                    <option value="bookmark_only">只收藏链接</option>
                  </select>
                </label>
                <p className="small muted">
                  只支持可公开访问的网页。登录墙、付费墙页面不会显示为已入库。
                </p>
              </>
            ) : (
              <>
                <label className="file-drop">
                  <Upload size={29} />
                  <strong>{file?.name || "选择一份资料"}</strong>
                  <span>文本型 PDF / DOCX · 最大 20 MiB</span>
                  <input
                    type="file"
                    accept=".pdf,.docx"
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) {
                        const invalid = validateFile(f);
                        setError(invalid || "");
                        setFile(invalid ? null : f);
                      }
                    }}
                  />
                </label>
                <p className="small muted">
                  首版暂不支持扫描件 OCR 与旧版 DOC。PDF
                  是否包含可提取文字，以后端解析结果为准。
                </p>
              </>
            )}
            <label>
              自定义标题 · 可选
              <input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="留空时使用原标题"
              />
            </label>
            {error && <Notice error>{error}</Notice>}
            <Button busy={busy} className="primary">
              {tab === "link" ? "收下这个链接" : "上传并开始处理"}
              <ArrowIcon />
            </Button>
          </form>
        </section>
        <section className="panel knowledge-queue">
          <h2>处理队列</h2>
          <p className="small muted">
            上传完成后，还需要抓取、解析与向量化。
            {isDemo && "这里展示的是模拟处理状态。"}
          </p>
          {allJobs.length ? (
            allJobs.map((job) => <JobCard key={job.id} initial={job} />)
          ) : (
            <Empty title="还没有处理中的资料">
              从一个网页链接开始，就很好。
            </Empty>
          )}
        </section>
      </div>
      <section className="panel library">
        <div className="section-heading">
          <h2>资料架</h2>
          <span className="heading-rule" />
        </div>
        {resources.error && <Notice error>{resources.error.message}</Notice>}
        {resources.data?.items.map((r) => (
          <div className="library-row" key={r.id}>
            <div className="row">
              <BookOpen size={17} />
              <div>
                <strong>{r.current_revision.title}</strong>
                <p className="small muted">
                  {kindLabel[r.kind]} ·{" "}
                  {r.publication ? "有独立公开版本" : "私人资料"}
                </p>
              </div>
            </div>
            <Button
              disabled={!r.current_revision.url}
              className="compact"
              onClick={async () => {
                try {
                  const result = await mutate<{ job: Job }>(
                    `/resources/${r.id}/refresh`,
                    { expected_version: r.version },
                    "POST",
                    newKey(),
                  );
                  setJobs([result.job, ...jobs]);
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              <RotateCcw size={13} />
              重新处理
            </Button>
          </div>
        ))}
      </section>
    </>
  );
}
function ArrowIcon() {
  return <ExternalLink size={15} />;
}
function JobCard({ initial }: { initial: Job }) {
  const [error, setError] = useState("");
  const query = useQuery({
    queryKey: ["job", initial.id],
    queryFn: () => api<Job>(`/jobs/${initial.id}`),
    initialData: initial,
    refetchInterval: (q) =>
      ["queued", "running", "cancelling"].includes(q.state.data?.status || "")
        ? 1800
        : false,
  });
  const job = query.data,
    labels = {
      queued: "排队中",
      running: "处理中",
      waiting_auth: "需要重新验证",
      succeeded: "处理完成",
      failed: "处理失败",
      cancelling: "正在取消",
      cancelled: "已取消",
    },
    phases = {
      fetching: "抓取网页",
      parsing: "解析文字",
      embedding: "建立检索索引",
    };
  return (
    <div className="job-card inset">
      <div className="row between">
        <span className="small">任务 {job.id.slice(0, 8)}</span>
        <span className={`pill ${job.status === "succeeded" ? "green" : ""}`}>
          {labels[job.status]}
        </span>
      </div>
      <p className="muted small">
        {job.status === "running" && job.phase
          ? phases[job.phase]
          : job.status === "succeeded"
            ? "资料处理已完成。是否公开需另行设置。"
            : labels[job.status]}
      </p>
      {job.progress !== null && (
        <progress max={100} value={job.progress}>
          {job.progress}%
        </progress>
      )}
      {query.error && <Notice error>{query.error.message}</Notice>}
      {job.error && <Notice error>{job.error.message}</Notice>}
      {error && <Notice error>{error}</Notice>}
      <div className="row">
        {["queued", "running"].includes(job.status) && (
          <button
            className="text-button small"
            onClick={async () => {
              try {
                await mutate(`/jobs/${job.id}/cancel`);
                query.refetch();
              } catch (e) {
                setError((e as Error).message);
              }
            }}
          >
            取消处理
          </button>
        )}
        {job.can_retry && (
          <button
            className="text-button small"
            onClick={async () => {
              try {
                await mutate(`/jobs/${job.id}/retry`, {}, "POST", newKey());
                query.refetch();
              } catch (e) {
                setError((e as Error).message);
              }
            }}
          >
            重新尝试
          </button>
        )}
      </div>
    </div>
  );
}
export function ModerationAdmin() {
  const session = useSession(),
    query = useQuery({
      queryKey: ["moderation", session.user!.id],
      queryFn: () => api<Page<Comment>>("/moderation/comments?status=pending"),
    }),
    reports = useQuery({
      queryKey: ["reports", session.user!.id],
      queryFn: () => api<Page<Report>>("/moderation/reports"),
    }),
    [error, setError] = useState("");
  const decide = async (c: Comment, decision: string) => {
    try {
      await mutate(`/moderation/comments/${c.id}/decision`, {
        decision,
        reason: "站长审核",
        expected_version: c.version,
      });
      query.refetch();
      session.toast(decision === "approve" ? "留言已公开。" : "留言已拒绝。");
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <>
      <PageHeading eyebrow="WORKSPACE / MODERATION" title="照看这个小角落">
        给善意留一扇窗，也为公开交流守好边界。
      </PageHeading>
      {error && <Notice error>{error}</Notice>}
      <section className="panel moderation-panel">
        <h2>待审核留言</h2>
        {query.error && <Notice error>{query.error.message}</Notice>}
        {query.data?.items.length ? (
          query.data.items.map((c) => (
            <div className="moderation-row" key={c.id}>
              <div>
                <p>
                  <strong>{c.author_display_name}</strong>
                  <span className="small muted">
                    {" "}
                    · {formatDate(c.created_at)}
                  </span>
                </p>
                <p>{c.body}</p>
              </div>
              <div className="row">
                <Button
                  className="compact primary"
                  onClick={() => decide(c, "approve")}
                >
                  <ShieldCheck size={15} />
                  通过
                </Button>
                <Button className="compact" onClick={() => decide(c, "reject")}>
                  拒绝
                </Button>
              </div>
            </div>
          ))
        ) : (
          <Empty title="没有等待审核的短笺" />
        )}
      </section>
      <section className="panel moderation-panel">
        <h2>举报处理</h2>
        {reports.error && <Notice error>{reports.error.message}</Notice>}
        {reports.data?.items.length ? (
          reports.data.items.map((r) => (
            <div className="moderation-row" key={r.id}>
              <p>{r.reason}</p>
              <Button
                className="compact"
                onClick={async () => {
                  try {
                    await mutate(`/moderation/reports/${r.id}/resolve`, {
                      resolution: "reviewed",
                      expected_version: r.version,
                    });
                    reports.refetch();
                  } catch (e) {
                    setError((e as Error).message);
                  }
                }}
              >
                标记已处理
              </Button>
            </div>
          ))
        ) : (
          <Empty title="目前没有待处理的举报" />
        )}
      </section>
    </>
  );
}
export function SettingsAdmin() {
  const request = useIdempotentRequest();
  const session = useSession(),
    limits = useQuery({
      queryKey: ["limits", session.user!.id],
      queryFn: () => api<Limits>("/settings/ai-limits"),
    }),
    retention = useQuery({
      queryKey: ["retention", session.user!.id],
      queryFn: () =>
        api<{ mode: string; version: number; ttl_days?: number }>(
          "/settings/retention",
        ),
    }),
    memories = useQuery({
      queryKey: ["memories", session.user!.id],
      queryFn: () => api<Page<Memory>>("/memories"),
    }),
    audit = useQuery({
      queryKey: ["audit", session.user!.id],
      queryFn: () => api<Page<Audit>>("/audit-events"),
    });
  const [values, setValues] = useState<Limits["values"] | null>(null),
    [policy, setPolicy] = useState("forever"),
    [days, setDays] = useState(365),
    [existing, setExisting] = useState(false),
    [scope, setScope] = useState("conversations"),
    [action, setAction] = useState<Action | null>(null),
    [memory, setMemory] = useState(""),
    [error, setError] = useState("");
  const v = values || limits.data?.values;
  return (
    <>
      <PageHeading eyebrow="WORKSPACE / SETTINGS" title="按自己的节奏生长">
        额度、保存策略与手动记忆，都在这里调整。
      </PageHeading>
      {error && <Notice error>{error}</Notice>}
      <div className="settings-grid">
        <section className="panel settings-card">
          <h2>普通用户 AI 额度</h2>
          {limits.error && <Notice error>{limits.error.message}</Notice>}
          {v && (
            <form
              className="form-stack"
              onSubmit={async (e) => {
                e.preventDefault();
                try {
                  await mutate(
                    "/settings/ai-limits",
                    {
                      expected_version: limits.data!.version,
                      scope: "member",
                      values: v,
                    },
                    "PATCH",
                  );
                  await limits.refetch();
                  setValues(null);
                  session.toast("额度设置已保存。");
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              {(
                [
                  ["daily_limit", "每日次数", 1, 1000],
                  ["cooldown_hours", "邮箱验证后冷却（小时）", 0, 720],
                  ["per_minute", "每分钟请求数", 1, 100],
                  ["concurrency", "同时运行任务数", 1, 10],
                ] as const
              ).map(([key, label, min, max]) => (
                <label key={key}>
                  {label}
                  <input
                    type="number"
                    required
                    min={min}
                    max={max}
                    value={v[key]}
                    onChange={(e) =>
                      setValues({ ...v, [key]: Number(e.target.value) })
                    }
                  />
                </label>
              ))}
              <Button className="primary">保存额度</Button>
            </form>
          )}
        </section>
        <section className="panel settings-card">
          <h2>内容保存策略</h2>
          <p className="small muted">
            当前策略：
            {retention.data?.mode === "forever"
              ? "永久保存"
              : retention.data
                ? `${retention.data.ttl_days} 天`
                : "正在读取"}
            。修改前会生成影响预览。
          </p>
          {action ? (
            <ActionPreview
              action={action}
              onCancel={() => setAction(null)}
              onDone={() => {
                retention.refetch();
                audit.refetch();
              }}
            />
          ) : (
            <form
              className="form-stack"
              onSubmit={async (e) => {
                e.preventDefault();
                try {
                  setAction(
                    await request<Action>("/settings/retention/preview", {
                      expected_version: retention.data!.version,
                      scope,
                      mode: policy,
                      ttl_days: policy === "ttl" ? days : null,
                      include_existing: existing,
                    }),
                  );
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              <label>
                内容范围
                <select
                  value={scope}
                  onChange={(e) => setScope(e.target.value)}
                >
                  <option value="conversations">聊天</option>
                  <option value="articles">文章</option>
                  <option value="bookmarks">收藏</option>
                  <option value="logs">业务日志</option>
                </select>
              </label>
              <label>
                保存期限
                <select
                  value={policy}
                  onChange={(e) => setPolicy(e.target.value)}
                >
                  <option value="forever">永久</option>
                  <option value="ttl">指定天数</option>
                </select>
              </label>
              {policy === "ttl" && (
                <label>
                  保存天数
                  <input
                    type="number"
                    required
                    min={1}
                    max={36500}
                    value={days}
                    onChange={(e) => setDays(Number(e.target.value))}
                  />
                </label>
              )}
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={existing}
                  onChange={(e) => setExisting(e.target.checked)}
                />
                同时处理已有内容
              </label>
              <Button disabled={!retention.data}>预览策略变化</Button>
            </form>
          )}
        </section>
        <section className="panel settings-card">
          <h2>助手的手动记忆</h2>
          <p className="small muted">
            只保存你明确添加的偏好。普通用户不会自动生成个性档案。
          </p>
          <form
            className="form-stack"
            onSubmit={async (e) => {
              e.preventDefault();
              try {
                await request("/memories", {
                  content: memory,
                  kind: "preference",
                });
                setMemory("");
                memories.refetch();
              } catch (e) {
                setError((e as Error).message);
              }
            }}
          >
            <label>
              新增记忆
              <textarea
                required
                maxLength={2000}
                rows={3}
                value={memory}
                onChange={(e) => setMemory(e.target.value)}
                placeholder="例如：整理资料时先保留来源链接。"
              />
            </label>
            <Button>添加记忆</Button>
          </form>
          {memories.data?.items.map((m) => (
            <div className="memory-row" key={m.id}>
              <p>{m.content}</p>
              <button
                className="icon-button"
                aria-label="删除这条记忆"
                onClick={async () => {
                  try {
                    await mutate(
                      `/memories/${m.id}`,
                      { expected_version: m.version },
                      "DELETE",
                    );
                    memories.refetch();
                  } catch (e) {
                    setError((e as Error).message);
                  }
                }}
              >
                <Trash2 size={15} />
              </button>
            </div>
          ))}
        </section>
        <section className="panel settings-card">
          <h2>最近的操作</h2>
          <p className="small muted">公开与策略变更留有审计记录。</p>
          {audit.data?.items.length ? (
            audit.data.items.map((a) => (
              <div className="audit-row" key={a.id}>
                <p>{a.summary}</p>
                <span className="small muted">{formatDate(a.created_at)}</span>
              </div>
            ))
          ) : (
            <Empty title="还没有操作记录" />
          )}
          <div className="inset small muted">
            当前角色方向：停云，暖白 / 赭红 /
            柔金。减少动画会跟随设备偏好，聊天中的小形象可以收起。
          </div>
        </section>
      </div>
    </>
  );
}

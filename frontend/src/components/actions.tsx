"use client";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, isDemo, mutate } from "@/lib/api";
import { useIdempotentRequest } from "@/lib/use-request";
import { useSession } from "./providers";
import type { Action } from "@/lib/contracts";
import { Button, Notice } from "./ui";
export function ActionPreview({
  action: initial,
  onDone,
  onCancel,
}: {
  action: Action;
  onDone?: () => void;
  onCancel?: () => void;
}) {
  const [action, setAction] = useState(initial),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [confirmed, setConfirmed] = useState(false);
  const request = useIdempotentRequest(),
    session = useSession(),
    completed = useRef<string | null>(null);
  const progress = useQuery({
    queryKey: ["action", session.user?.id, "owner", action.id],
    queryFn: () => api<Action>(`/actions/${action.id}`),
    enabled: action.status === "running",
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1500 : false),
  });
  useEffect(() => {
    if (progress.data) {
      setAction(progress.data);
      if (
        progress.data.status === "succeeded" &&
        completed.current !== progress.data.id
      ) {
        completed.current = progress.data.id;
        onDone?.();
      }
    }
  }, [progress.data, onDone]);
  const execute = async () => {
    setBusy(true);
    setError("");
    try {
      const next = await request<Action>(`/actions/${action.id}/execute`, {
        parameters_hash: action.parameters_hash,
      });
      setAction(next);
      if (next.status === "succeeded") {
        completed.current = next.id;
        onDone?.();
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const poll = async () => {
    try {
      const next = await api<Action>(`/actions/${action.id}`);
      setAction(next);
      if (next.status === "succeeded") onDone?.();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <div className="action-preview">
      <p className="eyebrow">REVIEW BEFORE CONTINUING</p>
      <h3>{action.summary}</h3>
      <p className="muted">{action.impact}</p>
      <div className="inset">
        <p className="small muted">
          目标版本 {action.expected_version}
          {action.expected_acl_version !== undefined &&
            ` · 权限版本 ${action.expected_acl_version}`}
        </p>
        <dl className="change-list">
          {Object.entries(action.changes)
            .filter(
              ([key]) =>
                ![
                  "revision_id",
                  "expected_version",
                  "expected_acl_version",
                ].includes(key),
            )
            .map(([key, value]) => (
              <div key={key}>
                <dt>
                  {{
                    public_fields: "公开字段",
                    ai_enabled: "允许问答引用",
                    raw_download_enabled: "原文件下载",
                    revision_id: "内容版本",
                    expected_version: "目标版本",
                    expected_acl_version: "权限版本",
                    scope: "范围",
                    mode: "保存方式",
                    ttl_days: "保存天数",
                    include_existing: "处理已有内容",
                  }[key] || key}
                </dt>
                <dd>
                  {typeof value === "boolean"
                    ? value
                      ? "允许"
                      : "不允许"
                    : Array.isArray(value)
                      ? value
                          .map(
                            (v) =>
                              ({
                                title: "标题",
                                body_text: "正文",
                                url: "网址",
                                tags: "标签",
                                private_note: "私人备注",
                              })[String(v)] || String(v),
                          )
                          .join("、")
                      : String(value)}
                </dd>
              </div>
            ))}
        </dl>
      </div>
      {error && <Notice error>{error}</Notice>}
      {progress.error && <Notice error>{progress.error.message}</Notice>}
      {action.status === "succeeded" ? (
        <>
          <Notice>
            {isDemo ? "演示操作完成。真实数据尚未写入后端。" : "操作已完成。"}
          </Notice>
          {action.can_undo && (
            <Button
              onClick={async () => {
                try {
                  const next = await request<Action>(
                    `/actions/${action.id}/undo`,
                    {},
                  );
                  setAction(next);
                  setConfirmed(false);
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              预览撤销操作
            </Button>
          )}
        </>
      ) : action.status === "running" ? (
        <div className="row">
          <Notice>操作执行中，请等待后端确认。</Notice>
          <Button onClick={poll}>查看结果</Button>
        </div>
      ) : (
        <>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            我已检查变化和影响范围
          </label>
          <div className="row wrap">
            <Button
              className="primary"
              busy={busy}
              disabled={
                !confirmed || ["cancelled", "expired"].includes(action.status)
              }
              onClick={execute}
            >
              确认执行
            </Button>
            <Button
              disabled={busy}
              onClick={async () => {
                try {
                  await mutate(`/actions/${action.id}/cancel`);
                  onCancel?.();
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              取消
            </Button>
          </div>
        </>
      )}
    </div>
  );
}

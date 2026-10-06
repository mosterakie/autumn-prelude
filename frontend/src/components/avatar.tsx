"use client";
import { useEffect, useState } from "react";
import type { RunStatus } from "@/lib/contracts";
export function AssistantAvatar({
  status,
  focused,
  working,
}: {
  status?: RunStatus;
  focused?: boolean;
  working?: boolean;
}) {
  const [hidden, setHidden] = useState(false),
    [displayStatus, setDisplayStatus] = useState(status);
  useEffect(() => {
    setDisplayStatus(status);
    if (status && ["succeeded", "failed", "cancelled"].includes(status)) {
      const timer = setTimeout(() => setDisplayStatus(undefined), 2200);
      return () => clearTimeout(timer);
    }
  }, [status]);
  const label = working
    ? "正在查阅资料"
    : displayStatus === "waiting_auth"
      ? "等待验证"
      : displayStatus === "waiting_approval"
        ? "等待确认"
        : displayStatus === "waiting_input"
          ? "等你补充"
          : ["running", "queued"].includes(displayStatus || "")
            ? "认真思考中"
            : displayStatus === "cancelling"
              ? "正在停止"
              : displayStatus === "succeeded"
                ? "回答已完成"
                : displayStatus === "failed"
                  ? "遇到一点问题"
                  : displayStatus === "cancelled"
                    ? "回答已停止"
                    : focused
                      ? "在听你说"
                      : "停云 · 待机";
  if (hidden)
    return (
      <button
        className="small muted text-button"
        onClick={() => setHidden(false)}
      >
        显示小助手
      </button>
    );
  return (
    <div
      className={`assistant-avatar ${working || displayStatus === "running" ? "thinking" : ""}`}
    >
      <img
        className="assistant-sprite"
        src="/images/tingyun-pixel.png"
        alt="停云像素小形象"
        width={96}
        height={112}
        decoding="async"
      />
      <span className="small">{label}</span>
      <button
        className="icon-button"
        aria-label="收起小助手"
        onClick={() => setHidden(true)}
      >
        ×
      </button>
    </div>
  );
}

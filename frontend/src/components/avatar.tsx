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
      <svg
        viewBox="0 0 24 26"
        role="img"
        aria-label="停云像素小形象"
        shapeRendering="crispEdges"
      >
        <path fill="#654338" d="M4 2h3v2h3v3h5V4h3V2h3v8h1v12H2V10h2z" />
        <path fill="#bd8a68" d="M5 4h2v4H5zm13 0h2v4h-2z" />
        <path fill="#f2d6b5" d="M6 10h12v9H6z" />
        <path fill="#754a39" d="M4 8h15v4H9v2H6v-3H4z" />
        <path fill="#416a54" d="M8 14h2v2H8zm7 0h2v2h-2z" />
        <path fill="#cf8f81" d="M7 17h3v1H7zm8 0h3v1h-3z" />
        <path fill="#864739" d="M11 18h3v1h-3z" />
        <path fill="#923f35" d="M5 21h14v4H5z" />
        <path fill="#cfb075" d="M10 20h5v2h-5zm7-12h3v2h-3z" />
        <path fill="#527965" d="M18 10h2v3h-2z" />
      </svg>
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

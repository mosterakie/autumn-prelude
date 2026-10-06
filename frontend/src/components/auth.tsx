"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { isDemo, mutate } from "@/lib/api";
import { safeReturnPath } from "@/lib/safety";
import { mailToken } from "@/lib/mail-token";
import { useSession } from "./providers";
import { Button, Notice, PageHeading, Spinner } from "./ui";
export type AuthKind =
  | "login"
  | "register"
  | "verify-email"
  | "forgot-password"
  | "reset-password"
  | "resend-verification"
  | "step-up";
export function AuthForm({
  kind,
  onSuccess,
  token: initialToken = "",
}: {
  kind: AuthKind;
  onSuccess?: () => void;
  token?: string;
}) {
  const session = useSession(),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [email, setEmail] = useState(session.user?.email || ""),
    [password, setPassword] = useState(""),
    [displayName, setDisplayName] = useState(""),
    [token, setToken] = useState(initialToken),
    [totp, setTotp] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const body =
        kind === "login"
          ? { email, password }
          : kind === "register"
            ? { email, password, display_name: displayName }
            : kind === "reset-password"
              ? { token, new_password: password }
              : kind === "verify-email"
                ? { token }
                : kind === "step-up"
                  ? { totp_code: totp }
                  : { email };
      await mutate(
        `/auth/${kind === "resend-verification" ? "resend-verification" : kind}`,
        body,
      );
      if (["login", "step-up"].includes(kind)) {
        await session.refresh();
        onSuccess?.();
      } else
        setSuccess(
          isDemo
            ? "演示流程已完成。此预览不会发送邮件，也不会创建真实账号。"
            : kind === "verify-email"
              ? "邮箱验证成功，AI 冷却期从验证完成时开始。"
              : kind === "reset-password"
                ? "密码已更新，请重新登录。"
                : "请求已受理。如果邮箱符合条件，你将收到邮件，请检查收件箱。",
        );
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
      setPassword("");
      setTotp("");
    }
  };
  return (
    <form onSubmit={submit} className="form-stack">
      {isDemo && (
        <Notice>
          当前为演示模式，请勿填写真实密码。
          {kind === "step-up"
            ? "演示验证码：123456。"
            : "体验邮箱：visitor@example.com；站长预览：owner@example.com。密码填写任意 8 位字符。"}
        </Notice>
      )}
      {["login", "register", "forgot-password", "resend-verification"].includes(
        kind,
      ) && (
        <label>
          邮箱
          <input
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
          />
        </label>
      )}
      {kind === "register" && (
        <label>
          怎么称呼你
          <input
            required
            maxLength={40}
            autoComplete="nickname"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="一个喜欢的名字"
          />
        </label>
      )}
      {["login", "register", "reset-password"].includes(kind) && (
        <label>
          {kind === "reset-password" ? "新密码" : "密码"}
          <input
            type="password"
            required
            minLength={isDemo ? 8 : 12}
            maxLength={isDemo ? 128 : 256}
            autoComplete={
              kind === "login" ? "current-password" : "new-password"
            }
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={isDemo ? "至少 8 个字符" : "至少 12 个字符"}
          />
        </label>
      )}
      {["verify-email", "reset-password"].includes(kind) && !initialToken && (
        <label>
          邮件中的一次性凭据
          <input
            required
            value={token}
            onChange={(e) => setToken(e.target.value)}
            autoComplete="off"
          />
        </label>
      )}
      {kind === "step-up" && (
        <label>
          验证码
          <input
            inputMode="numeric"
            pattern="[0-9]{6}"
            maxLength={6}
            autoComplete="one-time-code"
            required
            value={totp}
            onChange={(e) => setTotp(e.target.value)}
            placeholder="六位验证码"
          />
        </label>
      )}
      {error && <Notice error>{error}</Notice>}
      {success && <Notice>{success}</Notice>}
      <Button busy={busy} className="primary">
        {
          {
            login: "登录",
            register: "创建账号",
            "verify-email": "验证邮箱",
            "forgot-password": "发送重置邮件",
            "reset-password": "更新密码",
            "resend-verification": "重新发送验证邮件",
            "step-up": "验证并继续",
          }[kind]
        }
      </Button>
      {kind === "login" && (
        <div className="row between small">
          <Link href="/register">还没有账号？注册</Link>
          <Link href="/forgot-password">忘记密码</Link>
        </div>
      )}
      {kind === "register" && (
        <>
          <p className="small muted">
            验证邮箱后等待 24 小时即可体验 AI，每日 10 次。留言无需等待 AI
            冷却。
          </p>
          <Link className="small" href="/login">
            已有账号，去登录
          </Link>
        </>
      )}
      {success && !["login", "step-up"].includes(kind) && (
        <Link href="/login">返回登录</Link>
      )}
    </form>
  );
}
export function AuthPage({ kind }: { kind: AuthKind }) {
  const search = useSearchParams(),
    router = useRouter(),
    [linkToken, setLinkToken] = useState<string | null>(null),
    capturedKind = useRef<AuthKind | null>(null);
  const isMailLink = ["verify-email", "reset-password"].includes(kind);
  useEffect(() => {
    if (!["verify-email", "reset-password"].includes(kind)) return;
    // Strict Mode 会再次运行 effect，不能用已清理的 URL 覆盖捕获的凭据。
    if (capturedKind.current === kind) return;
    capturedKind.current = kind;
    setLinkToken(mailToken(window.location.search, window.location.hash));
    const query = new URLSearchParams(window.location.search);
    query.delete("token");
    const clean = window.location.pathname + (query.size ? `?${query}` : "");
    window.history.replaceState(window.history.state, "", clean);
  }, [kind]);
  const titles: Record<AuthKind, string> = {
    login: "欢迎回到秋序",
    register: "在这里，留下名字",
    "verify-email": "确认你的邮箱",
    "forgot-password": "找回小站的钥匙",
    "reset-password": "重新设置密码",
    "resend-verification": "重新发送验证邮件",
    "step-up": "站长验证",
  };
  return (
    <div className="auth-page">
      <PageHeading eyebrow="AUTUMN PRELUDE · ACCOUNT" title={titles[kind]}>
        每一段相遇，都从一句你好开始。
      </PageHeading>
      <div className="panel auth-card">
        {isMailLink && linkToken === null ? (
          <Spinner />
        ) : (
          <AuthForm
            kind={kind}
            token={isMailLink ? linkToken || "" : ""}
            onSuccess={() =>
              router.replace(safeReturnPath(search.get("returnTo")))
            }
          />
        )}
      </div>
    </div>
  );
}

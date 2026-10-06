"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Menu, X, ArrowUpRight, Sparkles, LogOut } from "lucide-react";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Quota } from "@/lib/contracts";
import { useSession, DemoBadge } from "./providers";
const navigation = [
  ["/notes", "手记"],
  ["/bookmarks", "收藏"],
  ["/guestbook", "留言"],
  ["/about", "关于"],
];
export function Header() {
  const path = usePathname(),
    session = useSession(),
    [open, setOpen] = useState(false);
  return (
    <header className="site-header">
      <div className="header-inner">
        <Link href="/" className="brand" aria-label="秋序首页">
          <span className="brand-seal">秋</span>
          <span className="brand-name">
            秋序<small>AUTUMN PRELUDE</small>
          </span>
        </Link>
        <nav aria-label="主导航" className={open ? "nav open" : "nav"}>
          {navigation.map(([href, label]) => (
            <Link
              key={href}
              href={href}
              aria-current={path.startsWith(href) ? "page" : undefined}
              onClick={() => setOpen(false)}
            >
              {label}
            </Link>
          ))}
          {session.user?.role === "owner" && (
            <Link href="/admin/content" onClick={() => setOpen(false)}>
              工作台
            </Link>
          )}
          <Link
            href={session.user?.role === "owner" ? "/admin/chat" : "/chat"}
            onClick={() => setOpen(false)}
            className="nav-chat"
          >
            <Sparkles size={15} />
            {session.user?.role === "owner" ? "私人助手" : "与助手聊聊"}
          </Link>
        </nav>
        <div className="row header-actions">
          {session.user ? (
            <Link href="/account" className="account-link">
              <span className="user-dot">{session.user.display_name[0]}</span>
              <span>{session.user.role === "owner" ? "站长" : "我的小站"}</span>
            </Link>
          ) : (
            <button className="button compact" onClick={session.openLogin}>
              登录 <span className="muted">/</span> 注册
            </button>
          )}
          <button
            className="icon-button mobile-menu"
            aria-label={open ? "关闭菜单" : "打开菜单"}
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            {open ? <X /> : <Menu />}
          </button>
        </div>
      </div>
    </header>
  );
}
export function Footer() {
  return (
    <footer className="site-footer">
      <div>
        <Link href="/" className="footer-brand">
          秋序
        </Link>
        <span className="muted small">把好奇留在这里。</span>
      </div>
      <div className="row">
        <DemoBadge />
        <a
          href="https://github.com/mosterakie/autumn-prelude"
          target="_blank"
          rel="noopener noreferrer"
        >
          GitHub <ArrowUpRight size={14} />
        </a>
        <span className="small muted">个人记录与 AI 实验</span>
      </div>
    </footer>
  );
}
export const adminLinks = [
  ["/admin/chat", "私人助手"],
  ["/admin/content", "内容管理"],
  ["/admin/knowledge", "知识库"],
  ["/admin/moderation", "留言审核"],
  ["/admin/settings", "站点设置"],
];
export function OwnerContentLinks({ kind }: { kind?: "article" | "bookmark" }) {
  const session = useSession();
  if (session.user?.role !== "owner") return null;
  return (
    <div className="row wrap">
      {(!kind || kind === "article") && (
        <Link className="button" href="/admin/content?create=article">
          写手记
        </Link>
      )}
      {(!kind || kind === "bookmark") && (
        <Link className="button" href="/admin/content?create=bookmark">
          收藏网址
        </Link>
      )}
    </div>
  );
}
export function AdminNav() {
  const path = usePathname(),
    session = useSession();
  return (
    <div className="admin-bar">
      <div className="row wrap">
        {adminLinks.map(([href, title]) => (
          <Link
            key={href}
            href={href}
            className={path.startsWith(href) ? "active" : ""}
          >
            {title}
          </Link>
        ))}
      </div>
      <span className="small muted">
        {session.ownerReady ? "额外验证有效" : "需要额外验证"}
      </span>
    </div>
  );
}
export function AccountPage() {
  const session = useSession(),
    [error, setError] = useState("");
  const quota = useQuery({
    queryKey: ["quota", session.user?.id],
    queryFn: () => api<Quota>("/me/quota"),
    enabled: !!session.user,
  });
  return (
    <div className="panel account-card">
      <p className="eyebrow">YOUR ACCOUNT</p>
      <h1>你好，{session.user?.display_name}。</h1>
      <p className="muted">{session.user?.email}</p>
      <div className="account-grid">
        <div className="inset">
          <span className="small muted">邮箱状态</span>
          <h3>{session.user?.verified_at ? "已验证" : "待验证"}</h3>
        </div>
        <div className="inset">
          <span className="small muted">账号身份</span>
          <h3>{session.user?.role === "owner" ? "站长" : "读者"}</h3>
        </div>
      </div>
      {quota.data && (
        <div className="notice" style={{ marginBottom: 24 }}>
          今日剩余 {quota.data.remaining} / {quota.data.daily_limit} 次 ·
          重置时间：{new Date(quota.data.next_reset_at).toLocaleString("zh-CN")}
          {quota.data.cooldown_until &&
            Date.parse(quota.data.cooldown_until) > session.serverNow() && (
              <p>
                冷却结束：
                {new Date(quota.data.cooldown_until).toLocaleString("zh-CN")}
              </p>
            )}
        </div>
      )}
      <div className="row wrap">
        <Link
          href={session.user?.role === "owner" ? "/admin/chat" : "/chat"}
          className="button primary"
        >
          继续对话 <Sparkles size={16} />
        </Link>
        {session.user?.role === "owner" && (
          <Link href="/admin/content" className="button">
            进入工作台
          </Link>
        )}
        <OwnerContentLinks />
        <button
          className="button"
          onClick={async () => {
            try {
              await session.logout();
            } catch (e) {
              setError((e as Error).message);
            }
          }}
        >
          <LogOut size={16} />
          退出登录
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
      <p className="small muted">
        文章、收藏和聊天默认永久保存。你可以管理自己的会话；站长可调整保存策略。
      </p>
    </div>
  );
}

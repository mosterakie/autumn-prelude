import Image from "next/image";
import Link from "next/link";
import { ArrowRight, ArrowUpRight, Sparkles, Leaf } from "lucide-react";
import { getNotes, getBookmarks } from "@/lib/server-data";
import { NoteList, BookmarkList } from "@/components/public";
import { DemoBadge } from "@/components/providers";
export const dynamic = "force-dynamic";
export default async function Home() {
  const [notes, bookmarks] = await Promise.all([getNotes(), getBookmarks()]);
  return (
    <>
      <section className="hero panel">
        <div className="hero-copy">
          <div className="row wrap">
            <span className="eyebrow">PERSONAL ARCHIVE / 01</span>
            <DemoBadge />
          </div>
          <h1>
            把好奇
            <br />
            留在这里<span>。</span>
          </h1>
          <p className="hero-subtitle">个人记录 / 网址收藏 / 与 AI 一起思考</p>
          <p className="hero-description">
            收藏值得回访的风景，记录尚未结束的思考。
            <br />
            一个慢慢生长的个人小站。
          </p>
          <div className="row wrap hero-buttons">
            <Link href="/notes" className="button primary">
              阅读手记
              <ArrowRight size={19} />
            </Link>
            <Link href="/chat" className="button">
              与助手聊聊
              <Sparkles size={17} />
            </Link>
          </div>
          <div className="hero-links">
            <a
              href="https://github.com/mosterakie/autumn-prelude"
              target="_blank"
              rel="noopener noreferrer"
            >
              GitHub
              <ArrowUpRight size={15} />
            </a>
            <Link href="/about">
              关于这个小站
              <ArrowUpRight size={15} />
            </Link>
          </div>
        </div>
        <div className="hero-art">
          <div className="art-disc" />
          <div className="art-line line-one" />
          <div className="art-line line-two" />
          <span className="art-star star-one">✧</span>
          <span className="art-star star-two">✦</span>
          <Image
            src="/images/tingyun-hero.png"
            alt="持折扇的停云角色风格插画"
            fill
            priority
            sizes="(max-width: 760px) 100vw, 55vw"
            className="hero-character"
          />
          <div className="hero-assistant">
            <div className="row between">
              <span>
                <span className="status-dot" />
                停云 · 秋序助手
              </span>
              <span className="pill">公开问答</span>
            </div>
            <Link href="/chat">
              想了解这个小站？
              <span className="round-arrow">
                <ArrowRight size={19} />
              </span>
            </Link>
          </div>
        </div>
      </section>
      <div className="home-grid">
        <section className="panel home-notes">
          <div className="section-heading">
            <h2>
              <span className="section-number">01</span> 最近手记
            </h2>
            <span className="heading-rule" />
            <Link href="/notes" aria-label="查看所有手记">
              <ArrowUpRight size={21} />
            </Link>
          </div>
          <NoteList initial={notes} compact />
        </section>
        <section className="panel home-bookmarks">
          <div className="section-heading">
            <h2>
              <span className="section-number">02</span> 网址收藏
            </h2>
            <span className="heading-rule" />
            <Link href="/bookmarks" aria-label="查看所有收藏">
              <ArrowUpRight size={21} />
            </Link>
          </div>
          <BookmarkList initial={bookmarks} compact />
        </section>
      </div>
      <section className="panel home-guestbook">
        <h2>
          <span className="section-number">03</span> 留言板
        </h2>
        <p>来过，就留下一点想法。</p>
        <Link className="button quiet" href="/guestbook">
          写封短笺
          <ArrowUpRight size={18} />
        </Link>
        <Leaf size={26} className="gold" />
      </section>
    </>
  );
}

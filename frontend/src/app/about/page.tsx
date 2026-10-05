import Link from "next/link";
import { ArrowUpRight, ArrowRight } from "lucide-react";
import { PageHeading } from "@/components/ui";
export const metadata = { title: "关于" };
export default function About() {
  return (
    <>
      <PageHeading eyebrow="ABOUT / AUTUMN PRELUDE" title="秋日前奏，慢慢展开">
        一个属于自己的空间，也欢迎路过的人。
      </PageHeading>
      <div className="about-grid">
        <div className="panel about-copy">
          <span className="about-seal">序</span>
          <h2>你好，这里是秋序。</h2>
          <p>
            中文名叫「秋序」，英文是 Autumn
            Prelude。像秋日的前奏，既是回望，也是一段新的开始。
          </p>
          <p>
            这里用来写手记、收藏网址、记录个人实验。一个 AI
            助手陪着寻找资料之间的联系，也帮助站长整理自己的知识库。
          </p>
          <p>
            公开内容可以自由阅读。注册后，访客可以留言并体验站内问答；私人资料和联网搜索，留在站长自己的工作台里。
          </p>
          <Link href="/guestbook" className="button primary">
            来打个招呼
            <ArrowRight size={17} />
          </Link>
        </div>
        <aside className="panel about-aside">
          <p className="eyebrow">FIND ME ELSEWHERE</p>
          <h2>另一扇窗</h2>
          <a
            href="https://github.com/mosterakie"
            target="_blank"
            rel="noopener noreferrer"
            className="link-row"
          >
            GitHub · mosterakie
            <ArrowUpRight size={19} />
          </a>
          <a
            href="https://github.com/mosterakie/autumn-prelude"
            target="_blank"
            rel="noopener noreferrer"
            className="link-row"
          >
            Autumn Prelude 仓库
            <ArrowUpRight size={19} />
          </a>
          <p className="small muted">
            本站仍在建设中。预览中的手记与留言是示例，角色插画为 AI
            生成的同人风格素材。
          </p>
          <div className="about-flower" aria-hidden="true">
            ✳
          </div>
        </aside>
      </div>
    </>
  );
}

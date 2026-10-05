"use client";
import { Button } from "@/components/ui";
export default function ErrorPage({ reset }: { reset: () => void }) {
  return (
    <div className="empty panel">
      <h2>这一页暂时无法打开</h2>
      <p>服务连接出现问题，请稍后再试。你的公开浏览无需登录。</p>
      <Button onClick={reset}>重新加载</Button>
    </div>
  );
}

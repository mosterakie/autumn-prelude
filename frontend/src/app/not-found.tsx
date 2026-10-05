import Link from "next/link";
export default function NotFound() {
  return (
    <div className="empty panel">
      <p className="eyebrow">404 / A MISSING PAGE</p>
      <h1>这一页，暂时没找到。</h1>
      <p>内容可能尚未公开，或已被收回。</p>
      <Link href="/" className="button primary">
        回到首页
      </Link>
    </div>
  );
}

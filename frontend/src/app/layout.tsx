import type { Metadata } from "next";
import { Providers } from "@/components/providers";
import { Header, Footer } from "@/components/shell";
import "./globals.css";
export const metadata: Metadata = {
  title: { default: "秋序 · Autumn Prelude", template: "%s · 秋序" },
  description: "把好奇留在这里。个人记录、网址收藏，与 AI 一起思考。",
  robots: {
    index: process.env.NEXT_PUBLIC_API_MODE === "api",
    follow: process.env.NEXT_PUBLIC_API_MODE === "api",
  },
};
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" data-scroll-behavior="smooth">
      <body>
        <Providers>
          <a className="skip-link" href="#main">
            跳到正文
          </a>
          <Header />
          <main id="main" className="site-main">
            {children}
          </main>
          <Footer />
        </Providers>
      </body>
    </html>
  );
}

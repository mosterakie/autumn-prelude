import { getBookmarks } from "@/lib/server-data";
import { BookmarkList } from "@/components/public";
import { PageHeading } from "@/components/ui";
import { OwnerContentLinks } from "@/components/shell";
export const metadata = { title: "收藏" };
export const dynamic = "force-dynamic";
export default async function Bookmarks() {
  return (
    <>
      <PageHeading
        eyebrow="02 / LINKS WORTH KEEPING"
        title="留一条路，下次再来"
        aside={<OwnerContentLinks kind="bookmark" />}
      >
        设计灵感、开发工具与阅读线索。每个链接，都有收藏的理由。
      </PageHeading>
      <BookmarkList initial={await getBookmarks()} />
    </>
  );
}

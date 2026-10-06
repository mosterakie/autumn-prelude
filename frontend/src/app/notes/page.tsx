import type { Metadata } from "next";
import { getNotes } from "@/lib/server-data";
import { NoteList } from "@/components/public";
import { PageHeading } from "@/components/ui";
import { OwnerContentLinks } from "@/components/shell";
export const metadata: Metadata = { title: "手记" };
export const dynamic = "force-dynamic";
export default async function Notes() {
  return (
    <>
      <PageHeading
        eyebrow="01 / MOMENTS & NOTES"
        title="每一次停笔，都是起笔"
        aside={<OwnerContentLinks kind="article" />}
      >
        关于日常、阅读，以及那些还在生长的想法。
      </PageHeading>
      <NoteList initial={await getNotes()} />
    </>
  );
}

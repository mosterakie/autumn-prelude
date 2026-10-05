import { getComments } from "@/lib/server-data";
import { Guestbook } from "@/components/public";
import { PageHeading } from "@/components/ui";
export const metadata = { title: "留言" };
export const dynamic = "force-dynamic";
export default async function Page() {
  return (
    <>
      <PageHeading eyebrow="03 / A NOTE FROM YOU" title="把相遇，写成一封短笺">
        不用长篇大论。一句问候，一点灵感，都很好。
      </PageHeading>
      <Guestbook initial={await getComments()} />
    </>
  );
}

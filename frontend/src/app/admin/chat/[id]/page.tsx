import { Suspense } from "react";
import { Chat } from "@/components/chat";
import { Spinner } from "@/components/ui";
export default async function Page({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return (
    <Suspense fallback={<Spinner />}>
      <Chat owner key={id} conversationId={id} />
    </Suspense>
  );
}

import { Suspense } from "react";
import { AuthGate } from "@/components/providers";
import { Chat } from "@/components/chat";
import { Spinner } from "@/components/ui";
export const metadata = {
  title: "对话",
  robots: { index: false, follow: false },
};
export default async function Page({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return (
    <AuthGate>
      <Suspense fallback={<Spinner />}>
        <Chat key={id} conversationId={id} />
      </Suspense>
    </AuthGate>
  );
}

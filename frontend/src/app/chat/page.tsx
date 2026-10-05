import { Suspense } from "react";
import { AuthGate } from "@/components/providers";
import { Chat } from "@/components/chat";
import { Spinner } from "@/components/ui";
export const metadata = {
  title: "与助手聊聊",
  robots: { index: false, follow: false },
};
export default function Page() {
  return (
    <AuthGate>
      <Suspense fallback={<Spinner />}>
        <Chat />
      </Suspense>
    </AuthGate>
  );
}

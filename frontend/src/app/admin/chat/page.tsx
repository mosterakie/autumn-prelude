import { Suspense } from "react";
import { Chat } from "@/components/chat";
import { Spinner } from "@/components/ui";
export default function Page() {
  return (
    <Suspense fallback={<Spinner />}>
      <Chat owner />
    </Suspense>
  );
}

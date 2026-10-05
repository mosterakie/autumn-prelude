import { Suspense } from "react";
import { AuthPage } from "@/components/auth";
import { Spinner } from "@/components/ui";
export const metadata = { title: "验证邮箱" };
export default function Page() {
  return (
    <Suspense fallback={<Spinner />}>
      <AuthPage kind="verify-email" />
    </Suspense>
  );
}

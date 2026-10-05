import { Suspense } from "react";
import { AuthPage } from "@/components/auth";
import { Spinner } from "@/components/ui";
export const metadata = { title: "找回密码" };
export default function Page() {
  return (
    <Suspense fallback={<Spinner />}>
      <AuthPage kind="forgot-password" />
    </Suspense>
  );
}

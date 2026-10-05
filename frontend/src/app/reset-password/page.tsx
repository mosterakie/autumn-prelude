import { Suspense } from "react";
import { AuthPage } from "@/components/auth";
import { Spinner } from "@/components/ui";
export const metadata = { title: "重置密码" };
export default function Page() {
  return (
    <Suspense fallback={<Spinner />}>
      <AuthPage kind="reset-password" />
    </Suspense>
  );
}

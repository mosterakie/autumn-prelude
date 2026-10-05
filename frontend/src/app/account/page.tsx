import { AuthGate } from "@/components/providers";
import { AccountPage } from "@/components/shell";
export const metadata = {
  title: "我的小站",
  robots: { index: false, follow: false },
};
export default function Page() {
  return (
    <AuthGate>
      <AccountPage />
    </AuthGate>
  );
}

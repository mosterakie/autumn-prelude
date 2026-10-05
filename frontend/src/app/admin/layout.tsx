import { AuthGate } from "@/components/providers";
import { AdminNav } from "@/components/shell";
export const metadata = {
  title: "站长工作台",
  robots: { index: false, follow: false },
};
export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGate owner>
      <AdminNav />
      {children}
    </AuthGate>
  );
}

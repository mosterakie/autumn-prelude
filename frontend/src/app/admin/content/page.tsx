import { ContentAdmin } from "@/components/admin";
export default async function ContentPage({
  searchParams,
}: {
  searchParams: Promise<{ create?: string }>;
}) {
  const { create } = await searchParams;
  const kind =
    create === "article" || create === "bookmark" ? create : undefined;
  return <ContentAdmin key={kind || "list"} initialKind={kind} />;
}

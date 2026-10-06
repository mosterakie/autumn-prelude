export function mailToken(search: string, hash: string): string {
  const value =
    new URLSearchParams(hash.replace(/^#/, "")).get("token") ??
    new URLSearchParams(search).get("token") ??
    "";
  return /^[A-Za-z0-9_-]{40,128}$/.test(value) ? value : "";
}

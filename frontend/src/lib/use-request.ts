"use client";
import { useRef } from "react";
import { mutate, newKey } from "./api";
// Preserve a request identity after an ambiguous transport failure, but give
// edited input or a deliberate later operation a fresh identity.
export function useIdempotentRequest() {
  const pending = useRef<{ signature: string; key: string } | null>(null);
  return async function request<T>(path: string, body: unknown): Promise<T> {
    let payload = body;
    if (body instanceof FormData) {
      payload = await Promise.all(
        Array.from(body.entries()).map(async ([name, value]) => [
          name,
          typeof value === "string"
            ? value
            : {
                name: value.name,
                digest: Array.from(
                  new Uint8Array(
                    await crypto.subtle.digest(
                      "SHA-256",
                      await value.arrayBuffer(),
                    ),
                  ),
                )
                  .map((b) => b.toString(16).padStart(2, "0"))
                  .join(""),
              },
        ]),
      );
    }
    const signature = JSON.stringify([path, payload]);
    if (!pending.current || pending.current.signature !== signature)
      pending.current = { signature, key: newKey() };
    const result = await mutate<T>(path, body, "POST", pending.current.key);
    pending.current = null;
    return result;
  };
}

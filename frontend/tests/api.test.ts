import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
beforeEach(() => {
  vi.resetModules();
  vi.stubEnv("NEXT_PUBLIC_API_MODE", "api");
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});
describe("FastAPI transport", () => {
  it("does not initialize the demo adapter on API pages in older browsers", async () => {
    vi.stubGlobal("structuredClone", undefined);
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ data: { user: null } })),
        ),
    );
    const { api } = await import("../src/lib/api");
    expect(await api("/auth/me")).toEqual({ user: null });
  });
  it("sends cookies, CSRF and the original idempotency key", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ data: { run_id: "r" }, request_id: "req-1" }),
          { status: 202 },
        ),
      );
    vi.stubGlobal("fetch", fetcher);
    const { api, setCsrf } = await import("../src/lib/api");
    setCsrf("test-csrf");
    const body = { message: "hello" };
    expect(
      await api("/ask", { method: "POST", body, key: "stable-key" }),
    ).toEqual({ run_id: "r" });
    const [url, options] = fetcher.mock.calls[0];
    expect(url).toBe("/api/ask");
    expect(options.credentials).toBe("include");
    expect(options.cache).toBe("no-store");
    expect(options.headers["X-CSRF-Token"]).toBe("test-csrf");
    expect(options.headers["Idempotency-Key"]).toBe("stable-key");
    expect(JSON.parse(options.body)).toEqual(body);
  });
  it("never falls back to demo data when the backend is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    const { api } = await import("../src/lib/api");
    await expect(api("/auth/me")).rejects.toMatchObject({
      code: "NETWORK_ERROR",
    });
  });
  it("preserves backend conflict and retry information", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              code: "VERSION_CONFLICT",
              message: "内容已变化",
              retry_after_seconds: 3,
            },
            request_id: "req-2",
          }),
          { status: 409 },
        ),
      ),
    );
    const { api } = await import("../src/lib/api");
    await expect(
      api("/resources/a", { method: "PATCH", body: { expected_version: 1 } }),
    ).rejects.toMatchObject({
      code: "VERSION_CONFLICT",
      status: 409,
      requestId: "req-2",
      retryAfter: 3,
    });
  });
  it("does not attach a JSON content type to multipart uploads", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ data: {} })));
    vi.stubGlobal("fetch", fetcher);
    const { api } = await import("../src/lib/api");
    const form = new FormData();
    form.append("file", new Blob(["test"]), "book.pdf");
    await api("/knowledge/files", {
      method: "POST",
      body: form,
      key: "upload-key",
    });
    expect(fetcher.mock.calls[0][1].headers["Content-Type"]).toBeUndefined();
  });
});

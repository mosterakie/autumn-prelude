import { webcrypto } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { randomId } from "../src/lib/uuid";

afterEach(() => vi.unstubAllGlobals());

describe("LAN request identities", () => {
  it("uses secure random bytes when randomUUID is unavailable", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: webcrypto.getRandomValues.bind(webcrypto),
    });
    const first = randomId(),
      second = randomId();
    expect(first).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    expect(first).not.toBe(second);
  });
  it("fails clearly instead of making weak random identities", () => {
    vi.stubGlobal("crypto", undefined);
    expect(() => randomId()).toThrow("浏览器无法生成请求标识");
  });
});

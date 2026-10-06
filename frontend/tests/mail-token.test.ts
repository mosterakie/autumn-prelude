import { describe, expect, it } from "vitest";
import { mailToken } from "../src/lib/mail-token";

describe("email link credentials", () => {
  it("reads fragment credentials and supports existing query links", () => {
    const value = "A".repeat(43);
    expect(mailToken("?returnTo=/account", `#token=${value}`)).toBe(value);
    expect(mailToken(`?token=${value}`, "")).toBe(value);
    expect(mailToken(`?token=${"B".repeat(43)}`, `#token=${value}`)).toBe(
      value,
    );
  });
  it("does not accept malformed or missing credentials", () => {
    expect(mailToken("", "")).toBe("");
    expect(mailToken("", "#token=short")).toBe("");
    expect(mailToken("", `#token=${"A".repeat(43)}%0A`)).toBe("");
  });
});

import { describe, expect, it } from "vitest";
import {
  applySnapshot,
  hideSources,
  normalizeRun,
  normalizeSnapshot,
  safeExternal,
  safeReturnPath,
  validateFile,
} from "../src/lib/safety";
import type { Message, Run } from "../src/lib/contracts";
const message: Message = {
  id: "a",
  role: "assistant",
  body: "累计文字",
  content_version: 3,
  status: "composing",
  created_at: "",
  citations: [],
};
describe("navigation and uploaded input", () => {
  it.each([
    "//evil.test",
    "/\\evil.test",
    "/%2f%2fevil.test",
    "/%5cevil.test",
    "https://evil.test",
    "/%0aevil",
    "/%zz",
  ])("rejects unsafe return path %s", (path) =>
    expect(safeReturnPath(path)).toBe("/account"),
  );
  it("preserves a local return target", () =>
    expect(safeReturnPath("/chat?resource=123")).toBe("/chat?resource=123"));
  it("does not render executable links", () => {
    expect(safeExternal("javascript:alert(1)")).toBeUndefined();
    expect(safeExternal("data:text/html,x")).toBeUndefined();
    expect(safeExternal("https://nextjs.org/docs")).toBe(
      "https://nextjs.org/docs",
    );
  });
  it("rejects old DOC and oversized files", () => {
    expect(validateFile({ name: "legacy.doc", size: 1 })).toBeTruthy();
    expect(
      validateFile({ name: "book.pdf", size: 21 * 1024 * 1024 }),
    ).toBeTruthy();
    expect(validateFile({ name: "book.DOCX", size: 1024 })).toBeNull();
  });
});
describe("replayed SSE snapshots", () => {
  it("maps the documented message_id payload", () => {
    expect(
      normalizeSnapshot({
        message_id: "a",
        body: "新的全文",
        content_version: 4,
      }).id,
    ).toBe("a");
  });
  it("replaces cumulative content rather than appending", () => {
    const next = applySnapshot([message], {
      ...message,
      body: "新的全文",
      content_version: 4,
    });
    expect(next).toHaveLength(1);
    expect(next[0].body).toBe("新的全文");
  });
  it("ignores replayed and older snapshots", () => {
    expect(applySnapshot([message], { ...message, body: "重复" })[0].body).toBe(
      message.body,
    );
    expect(
      applySnapshot([message], {
        ...message,
        body: "旧",
        content_version: 2,
      })[0].body,
    ).toBe(message.body);
  });
  it("never restores an invalidated message from a late snapshot", () => {
    const hidden = hideSources([message], ["a"]);
    expect(hidden[0].body).toBe("");
    expect(
      applySnapshot(hidden, { ...message, content_version: 8 })[0].status,
    ).toBe("hidden");
  });
  it("maps the RunDTO id and current_message", () => {
    const run = normalizeRun({
      id: "r",
      conversation_id: "c",
      status: "running",
      current_message: message,
    } as unknown as Run & { id: string; current_message: Message });
    expect(run.run_id).toBe("r");
    expect(run.message?.id).toBe("a");
  });
});

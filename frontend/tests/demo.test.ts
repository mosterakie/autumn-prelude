import { beforeEach, describe, expect, it } from "vitest";
import { demoRequest, resetDemo } from "../src/lib/demo";
import type {
  Action,
  AcceptedRun,
  Conversation,
  Identity,
  Page,
  Resource,
} from "../src/lib/contracts";
beforeEach(resetDemo);
const login = () =>
  demoRequest<Identity>("/auth/login", {
    method: "POST",
    body: { email: "owner@example.com", password: "preview123" },
  });
const stepUp = () =>
  demoRequest("/auth/step-up", {
    method: "POST",
    body: { totp_code: "123456" },
  });
describe("explicit demo workflows", () => {
  it("requires a separate owner step-up before private reads", async () => {
    await login();
    await expect(demoRequest("/resources", {})).rejects.toMatchObject({
      code: "STEP_UP_REQUIRED",
    });
    await stepUp();
    expect(
      (await demoRequest<Page<Resource>>("/resources", {})).items.length,
    ).toBeGreaterThan(0);
  });
  it("keeps a published version unchanged when a draft is edited", async () => {
    await login();
    await stepUp();
    const r = (await demoRequest<Page<Resource>>("/resources", {})).items[0];
    const updated = await demoRequest<Resource>(`/resources/${r.id}`, {
      method: "PATCH",
      body: { expected_version: r.version, title: "新的私人标题" },
    });
    expect(updated.current_revision.title).toBe("新的私人标题");
    expect(updated.publication?.title).toBe(r.publication?.title);
  });
  it("rejects execution of a preview after the resource changes", async () => {
    await login();
    await stepUp();
    const r = (await demoRequest<Page<Resource>>("/resources", {})).items[0];
    const a = await demoRequest<Action>(
      `/resources/${r.id}/publication/preview`,
      {
        method: "POST",
        body: {
          expected_version: r.version,
          public_fields: ["title"],
          ai_enabled: true,
        },
        key: "preview",
      },
    );
    await demoRequest(`/resources/${r.id}`, {
      method: "PATCH",
      body: { expected_version: r.version, title: "另一版本" },
    });
    await expect(
      demoRequest(`/actions/${a.id}/execute`, {
        method: "POST",
        body: { parameters_hash: a.parameters_hash },
        key: "execute",
      }),
    ).rejects.toMatchObject({ code: "VERSION_CONFLICT" });
  });
  it("retries one ask with the same key without adding a second run", async () => {
    await login();
    const c = await demoRequest<Conversation>("/conversations", {
      method: "POST",
      body: { mode: "public" },
    });
    const body = {
      conversation_id: c.id,
      client_message_id: crypto.randomUUID(),
      message: "你好",
      resource_ids: [],
      search_mode: "site",
    };
    const first = await demoRequest<AcceptedRun>("/ask", {
      method: "POST",
      body,
      key: "ask-key",
    });
    const retry = await demoRequest<AcceptedRun>("/ask", {
      method: "POST",
      body,
      key: "ask-key",
    });
    expect(first.run_id).toBe(retry.run_id);
    expect(retry.quota.used).toBe(1);
    await expect(
      demoRequest("/ask", {
        method: "POST",
        body: { ...body, message: "不同内容" },
        key: "ask-key",
      }),
    ).rejects.toMatchObject({ code: "IDEMPOTENCY_CONFLICT" });
  });
});

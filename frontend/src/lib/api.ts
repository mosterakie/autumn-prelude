import { randomId } from "./uuid";
export const isDemo = process.env.NEXT_PUBLIC_API_MODE !== "api";
let csrf: string | null = null;
export const setCsrf = (token: string | null) => {
  csrf = token;
};
export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status = 400,
    public requestId?: string,
    public retryAfter?: number,
  ) {
    super(message);
  }
}
export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  key?: string;
  signal?: AbortSignal;
}
export async function api<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  if (isDemo) {
    const { demoRequest } = await import("./demo");
    return demoRequest<T>(path, options);
  }
  const method = options.method || "GET",
    isForm = options.body instanceof FormData;
  const headers: Record<string, string> = {};
  if (options.body !== undefined && !isForm)
    headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrf) headers["X-CSRF-Token"] = csrf;
  if (options.key) headers["Idempotency-Key"] = options.key;
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers,
      credentials: "include",
      cache: "no-store",
      body:
        options.body === undefined
          ? undefined
          : isForm
            ? (options.body as FormData)
            : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new ApiError(
      "NETWORK_ERROR",
      "连接暂时中断，输入已保留。请重试原请求。",
      0,
    );
  }
  if (response.status === 204) return undefined as T;
  const value = await response.json().catch(() => null);
  if (!response.ok) {
    if (
      response.status === 401 &&
      typeof window !== "undefined" &&
      (!path.startsWith("/auth/") || path === "/auth/me")
    )
      window.dispatchEvent(new Event("autumn:expired"));
    throw new ApiError(
      value?.error?.code || "SERVICE_UNAVAILABLE",
      value?.error?.message || "服务暂时不可用，请稍后重试。",
      response.status,
      value?.request_id,
      value?.error?.retry_after_seconds,
    );
  }
  return value.data as T;
}
export const mutate = <T>(
  path: string,
  body?: unknown,
  method: "POST" | "PATCH" | "DELETE" = "POST",
  key?: string,
) => api<T>(path, { method, body, key });
export const newKey = randomId;

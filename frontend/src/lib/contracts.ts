export type Role = "member" | "owner";
export type Mode = "public" | "owner";
export interface User {
  id: string;
  display_name: string;
  email: string;
  role: Role;
  status: "pending_verification" | "active" | "disabled";
  verified_at: string | null;
  ai_cooldown_until: string | null;
  step_up_expires_at: string | null;
  capabilities: string[];
}
export interface Identity {
  user: User | null;
  csrf_token: string | null;
  server_time: string;
}
export interface Quota {
  timezone: string;
  daily_limit: number;
  used: number;
  reserved: number;
  remaining: number;
  cooldown_until: string | null;
  next_reset_at: string;
  server_time: string;
}
export interface PublicResource {
  id: string;
  kind: "article" | "bookmark" | "document" | "webpage";
  slug: string;
  publication_id: string;
  publication_no: number;
  title: string;
  body?: string;
  note?: string;
  url?: string;
  tags: string[];
  published_at: string;
  ai_enabled: boolean;
  raw_download_enabled: boolean;
}
export interface Comment {
  id: string;
  resource_id: string | null;
  parent_id: string | null;
  author_display_name: string;
  body: string;
  status: "pending" | "approved" | "rejected" | "hidden";
  created_at: string;
  version: number;
}
export interface Citation {
  id: string;
  title: string;
  url?: string;
  publication_id?: string;
  excerpt?: string;
  page?: number;
  section?: string;
}
export interface Message {
  id: string;
  role: "user" | "assistant";
  body: string;
  content_version: number;
  status: "composing" | "complete" | "interrupted" | "hidden";
  created_at: string;
  citations: Citation[];
}
export type RunStatus =
  | "queued"
  | "running"
  | "waiting_input"
  | "waiting_approval"
  | "waiting_auth"
  | "succeeded"
  | "failed"
  | "cancelling"
  | "cancelled";
export interface Conversation {
  id: string;
  title: string;
  mode: Mode;
  version: number;
  created_at: string;
  active_run_id?: string | null;
}
export interface Ask {
  conversation_id: string;
  client_message_id: string;
  message: string;
  resource_ids: string[];
  search_mode: "auto" | "site" | "web";
}
export interface Run {
  run_id: string;
  conversation_id: string;
  status: RunStatus;
  message?: Message;
  pending_actions?: Action[];
  input_request?: InputRequest | null;
}
export interface InputRequest {
  id: string;
  prompt: string;
  options: string[];
  expires_at?: string;
}
export interface AcceptedRun {
  run_id: string;
  conversation_id: string;
  status: RunStatus;
  events_url: string;
  quota: Quota;
}
export interface Revision {
  id: string;
  revision_no: number;
  title: string;
  body_text?: string;
  url?: string;
  private_note?: string;
  tags: string[];
}
export interface Resource {
  id: string;
  kind: PublicResource["kind"];
  slug: string;
  version: number;
  acl_version: number;
  current_revision: Revision;
  publication: PublicResource | null;
  processing_jobs?: Job[];
}
export interface Job {
  id: string;
  kind?: string;
  version?: number;
  resource_id: string;
  status:
    | "queued"
    | "running"
    | "waiting_auth"
    | "succeeded"
    | "failed"
    | "cancelling"
    | "cancelled";
  phase?:
    | "fetching"
    | "parsing"
    | "embedding"
    | "indexing"
    | "finalizing"
    | "deleting"
    | null;
  progress: number | null;
  can_retry: boolean;
  can_cancel?: boolean;
  error?: { code: string; message: string } | null;
}
export interface Action {
  id: string;
  version: number;
  type: string;
  target_id?: string | null;
  expected_version: number | null;
  expected_acl_version?: number | null;
  parameters_hash: string;
  summary: string;
  changes: Record<string, unknown>;
  impact: string;
  requires_confirmation: boolean;
  status:
    | "proposed"
    | "awaiting_confirmation"
    | "ready"
    | "running"
    | "succeeded"
    | "failed"
    | "cancelled"
    | "expired";
  expires_at: string;
  can_undo: boolean;
  result?: unknown;
}
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}
export interface Limits {
  version: number;
  values: {
    daily_limit: number;
    cooldown_hours: number;
    per_minute: number;
    concurrency: number;
  };
}
export interface Memory {
  id: string;
  content: string;
  version: number;
}
export interface Report {
  id: string;
  comment_id: string;
  reason: string;
  status: string;
  version: number;
}
export interface Audit {
  id: string;
  summary: string;
  created_at: string;
}
export const terminal = (s: RunStatus) =>
  ["succeeded", "failed", "cancelled"].includes(s);

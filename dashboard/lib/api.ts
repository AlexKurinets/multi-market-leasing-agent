// Server components call the API over the internal network; the browser uses the public URL.
const SERVER_BASE = process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
export const BROWSER_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Market = {
  id: string;
  name: string;
  timezone: string;
  agent_conversations: number;
  human_conversations: number;
};

export type ConversationSummary = {
  id: number;
  lead_id: number;
  lead_name: string;
  contact: string | null;
  lead_status: string;
  owner: "agent" | "human";
  channel: string;
  handoff_reason: string | null;
  last_message: string | null;
  last_message_at: string | null;
};

export type Message = {
  id: number;
  direction: "inbound" | "outbound";
  author: "lead" | "agent" | "human";
  channel: string;
  body: string;
  status: string;
  created_at: string;
};

export type ConversationDetail = ConversationSummary & {
  property: string | null;
  messages: Message[];
  tours: { property: string; starts_at: string; status: string }[];
};

export type Policy = { topic: string; summary: string };

export type Brief = {
  property: string;
  week_of: string;
  status: "approved" | "rejected";
  body: string;
  problems: string[];
};

export async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${SERVER_BASE}${path}`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`GET ${path} failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function formatTime(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

import Link from "next/link";

import { type ConversationSummary, formatTime, get } from "@/lib/api";

import { MarketNav } from "./market-nav";

export const dynamic = "force-dynamic";

type Props = {
  params: Promise<{ market: string }>;
  searchParams: Promise<{ owner?: string }>;
};

export default async function ConversationsPage({ params, searchParams }: Props) {
  const { market } = await params;
  const { owner } = await searchParams;
  const query = owner ? `?owner=${encodeURIComponent(owner)}` : "";
  const conversations = await get<ConversationSummary[]>(
    `/markets/${market}/conversations${query}`,
  );

  return (
    <>
      <h1>{owner === "human" ? "Needs a person" : "Conversations"}</h1>
      <MarketNav market={market} />
      {conversations.length === 0 ? (
        <p className="muted">No conversations yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Prospect</th>
              <th>Status</th>
              <th className="hide-sm">Last message</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {conversations.map((c) => (
              <tr key={c.id}>
                <td>
                  <Link href={`/markets/${market}/conversations/${c.id}`}>{c.lead_name}</Link>
                  <div className="muted">{c.contact}</div>
                </td>
                <td>
                  <span className={`badge ${c.owner}`}>{c.owner}</span>{" "}
                  <span className="muted">{c.lead_status.replace("_", " ")}</span>
                  {c.handoff_reason && <div className="muted">{c.handoff_reason}</div>}
                </td>
                <td className="hide-sm">{c.last_message}</td>
                <td className="muted">{formatTime(c.last_message_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

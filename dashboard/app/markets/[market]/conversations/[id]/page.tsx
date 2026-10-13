import { type ConversationDetail, formatTime, get } from "@/lib/api";

import { MarketNav } from "../../market-nav";
import { HumanControls } from "./human-controls";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ market: string; id: string }> };

export default async function ConversationPage({ params }: Props) {
  const { market, id } = await params;
  const c = await get<ConversationDetail>(`/markets/${market}/conversations/${id}`);

  return (
    <>
      <h1>{c.lead_name}</h1>
      <MarketNav market={market} />
      <div className="card">
        <span className={`badge ${c.owner}`}>{c.owner}</span>{" "}
        <span className="muted">
          {c.contact} · {c.channel} · {c.property ?? "no property chosen"} · {c.lead_status}
        </span>
        {c.handoff_reason && <p>Handed off: {c.handoff_reason}</p>}
        {c.tours.map((t) => (
          <p key={t.starts_at}>
            Tour at {t.property}, {formatTime(t.starts_at)} ({t.status})
          </p>
        ))}
      </div>

      <div className="transcript">
        {c.messages.map((m) => (
          <div key={m.id} className={`bubble ${m.direction}`}>
            <div>{m.body}</div>
            <div className="meta">
              {m.author} · {formatTime(m.created_at)}
              {m.direction === "outbound" && m.status !== "sent" && ` · ${m.status}`}
            </div>
          </div>
        ))}
      </div>

      <HumanControls market={market} conversationId={c.id} owner={c.owner} />
    </>
  );
}

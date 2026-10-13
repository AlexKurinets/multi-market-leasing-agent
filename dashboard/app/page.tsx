import Link from "next/link";

import { get, type Market } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function MarketsPage() {
  const markets = await get<Market[]>("/markets");
  return (
    <>
      <h1>Markets</h1>
      <div className="grid">
        {markets.map((m) => (
          <Link key={m.id} href={`/markets/${m.id}`} className="card">
            <strong>{m.name}</strong> <span className="muted">{m.timezone}</span>
            <p>
              <span className="badge">{m.agent_conversations} with agent</span>{" "}
              <span className="badge human">{m.human_conversations} need a person</span>
            </p>
          </Link>
        ))}
      </div>
    </>
  );
}

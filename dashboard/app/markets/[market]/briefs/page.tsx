import { type Brief, get } from "@/lib/api";

import { MarketNav } from "../market-nav";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ market: string }> };

export default async function BriefsPage({ params }: Props) {
  const { market } = await params;
  const briefs = await get<Brief[]>(`/markets/${market}/briefs`);
  return (
    <>
      <h1>Weekly briefs</h1>
      <MarketNav market={market} />
      {briefs.length === 0 && <p className="muted">No briefs yet. Run `leasing briefs`.</p>}
      {briefs.map((b) => (
        <div key={b.property} className="card">
          <strong>{b.property}</strong> <span className="muted">week of {b.week_of}</span>{" "}
          <span className={`badge ${b.status}`}>{b.status}</span>
          <p>{b.body}</p>
          {b.problems.length > 0 && (
            <p className="muted">Held for review: {b.problems.join("; ")}</p>
          )}
        </div>
      ))}
    </>
  );
}

import { get, type Policy } from "@/lib/api";

import { MarketNav } from "../market-nav";
import { PolicyEditor } from "./policy-editor";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ market: string }> };

export default async function PoliciesPage({ params }: Props) {
  const { market } = await params;
  const policies = await get<Policy[]>(`/markets/${market}/policies`);
  return (
    <>
      <h1>Market rules</h1>
      <MarketNav market={market} />
      <p className="muted">
        The agent quotes these word for word through its tools. Changes apply to the next message.
      </p>
      {policies.map((p) => (
        <PolicyEditor key={p.topic} market={market} policy={p} />
      ))}
    </>
  );
}

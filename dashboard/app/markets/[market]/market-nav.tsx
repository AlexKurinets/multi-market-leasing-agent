import Link from "next/link";

export function MarketNav({ market }: { market: string }) {
  return (
    <nav className="tabs">
      <Link href={`/markets/${market}`}>Conversations</Link>
      <Link href={`/markets/${market}?owner=human`}>Needs a person</Link>
      <Link href={`/markets/${market}/policies`}>Market rules</Link>
      <Link href={`/markets/${market}/briefs`}>Weekly briefs</Link>
    </nav>
  );
}

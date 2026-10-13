"use client";

import { useState } from "react";

import { BROWSER_BASE, type Policy } from "@/lib/api";

export function PolicyEditor({ market, policy }: { market: string; policy: Policy }) {
  const [summary, setSummary] = useState(policy.summary);
  const [saved, setSaved] = useState(policy.summary);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setError(null);
    const response = await fetch(`${BROWSER_BASE}/markets/${market}/policies/${policy.topic}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ summary }),
    });
    if (response.ok) setSaved(summary);
    else setError(`${response.status} ${await response.text()}`);
  }

  return (
    <div className="card">
      <strong>{policy.topic.replaceAll("_", " ")}</strong>
      <textarea value={summary} onChange={(e) => setSummary(e.target.value)} />
      <button disabled={summary === saved || summary.trim().length < 5} onClick={save}>
        Save
      </button>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { BROWSER_BASE } from "@/lib/api";

type Props = { market: string; conversationId: number; owner: "agent" | "human" };

export function HumanControls({ market, conversationId, owner }: Props) {
  const router = useRouter();
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const base = `${BROWSER_BASE}/markets/${market}/conversations/${conversationId}`;

  async function post(path: string, payload?: object) {
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`${base}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload ? JSON.stringify(payload) : undefined,
      });
      if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
      router.refresh();
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  if (owner === "agent") {
    return (
      <div className="card" style={{ marginTop: 16 }}>
        <p className="muted">The agent is handling this conversation.</p>
        <button className="secondary" disabled={busy} onClick={() => post("/takeover")}>
          Take over
        </button>
        {error && <p className="error">{error}</p>}
      </div>
    );
  }

  return (
    <form
      className="card"
      style={{ marginTop: 16 }}
      onSubmit={async (event) => {
        event.preventDefault();
        // One key per composed message, so a double-click cannot send twice.
        const sent = await post("/messages", { body, client_key: crypto.randomUUID() });
        if (sent) setBody("");
      }}
    >
      <textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder="Reply as the leasing team"
      />
      <button type="submit" disabled={busy || !body.trim()}>
        Send
      </button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

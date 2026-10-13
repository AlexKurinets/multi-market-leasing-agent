import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "Leasing Console",
  description: "Review agent conversations, edit market rules and read weekly briefs.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header>
          <strong>Leasing Console</strong>
          <Link href="/">Markets</Link>
          <span className="muted">Synthetic demo data</span>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}

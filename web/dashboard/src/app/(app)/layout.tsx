import type { ReactNode } from "react";
import { Sidebar } from "@/components/Sidebar";
import { requireSession } from "@/lib/server";

export default async function AppLayout({ children }: { children: ReactNode }) {
  const session = await requireSession();
  return (
    <div className="lg:flex">
      <a href="#main" className="sr-only z-50 rounded-pill bg-accent px-4 py-2 font-semibold focus:not-sr-only focus:fixed focus:left-4 focus:top-4">
        Skip to content
      </a>
      <Sidebar business={session.tenant} />
      <main id="main" className="min-w-0 flex-1 px-4 py-6 sm:px-8 lg:py-10">
        <div className="mx-auto max-w-6xl">{children}</div>
      </main>
    </div>
  );
}

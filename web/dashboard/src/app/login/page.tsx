import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { getSession } from "@/lib/server";
import { LoginForm } from "./LoginForm";

export const metadata: Metadata = { title: "Sign in" };

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ expired?: string }> }) {
  if (await getSession()) redirect("/");
  const { expired } = await searchParams;
  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-12">
      <div className="w-full max-w-md rounded-panel bg-surface p-8 shadow-panel">
        <div className="mb-6 flex items-center gap-3">
          <span aria-hidden="true" className="grid size-11 place-items-center rounded-2xl bg-accent text-lg font-black">
            B
          </span>
          <div>
            <h1 className="text-xl font-bold">Sign in to your dashboard</h1>
            <p className="text-sm text-muted">Paste an admin API key for your business.</p>
          </div>
        </div>
        {expired ? (
          <p role="status" className="mb-4 rounded-2xl bg-subtle px-4 py-3 text-sm">
            Your session ended. Please sign in again.
          </p>
        ) : null}
        <LoginForm />
        <p className="mt-6 text-xs text-muted">
          The key is checked with the platform and kept only in a secure, http-only cookie on this
          server&apos;s domain. It is never stored in your browser&apos;s storage.
        </p>
      </div>
    </main>
  );
}

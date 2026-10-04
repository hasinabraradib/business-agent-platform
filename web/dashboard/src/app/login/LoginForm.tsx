"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, inputClass } from "@/components/ui";

export function LoginForm() {
  const router = useRouter();
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/session", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ key }),
      });
      if (!response.ok) {
        setError(((await response.json().catch(() => ({}))) as { error?: string }).error ?? "Sign-in failed.");
        return;
      }
      setKey("");
      router.replace("/");
      router.refresh();
    } catch {
      setError("Can't reach the dashboard server. Check your connection.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="api-key" className="text-sm font-semibold">
          Admin API key
        </label>
        <input
          id="api-key"
          name="api-key"
          type="password"
          autoComplete="off"
          spellCheck={false}
          required
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="bap_admin_…"
          aria-describedby={error ? "login-error" : undefined}
          aria-invalid={error ? true : undefined}
          className={inputClass}
        />
      </div>
      {error ? (
        <p id="login-error" role="alert" className="rounded-2xl bg-danger-soft px-4 py-3 text-sm font-medium text-danger">
          {error}
        </p>
      ) : null}
      <Button type="submit" disabled={busy || !key.trim()} className="w-full">
        {busy ? "Checking…" : "Sign in"}
      </Button>
    </form>
  );
}

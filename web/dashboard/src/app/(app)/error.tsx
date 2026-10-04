"use client";
import { ErrorState } from "@/components/ui";

export default function AppError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  const message = error.message && !error.message.includes("fetch") ? error.message : "This page couldn't load. The platform may be unavailable.";
  return (
    <div className="py-6">
      <h1 className="mb-4 text-2xl font-bold">Something went wrong</h1>
      <ErrorState message={message} onRetry={reset} />
    </div>
  );
}

"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Badge, EmptyState, ErrorState, Loading, inputClass } from "@/components/ui";
import { request } from "@/lib/client";
import { STATUS_LABELS, channelLabel, customerName, relativeTime } from "@/lib/format";
import type { Conversation, ConversationStatus } from "@/lib/types";
import { usePolling } from "@/lib/usePolling";

const FILTERS: { value: ConversationStatus | ""; label: string }[] = [
  { value: "", label: "All" },
  { value: "waiting_human", label: "Waiting for team" },
  { value: "human", label: "With team" },
  { value: "ai", label: "Assistant" },
  { value: "resolved", label: "Resolved" },
];

export const STATUS_TONE: Record<ConversationStatus, "violet" | "ink" | "neutral" | "accent"> = {
  waiting_human: "violet",
  human: "ink",
  ai: "neutral",
  resolved: "accent",
};

export function inboxQuery(status: string, search: string): string {
  const params = new URLSearchParams({ limit: "100" });
  if (status) params.set("status", status);
  if (search.trim()) params.set("q", search.trim());
  return `conversations?${params}`;
}

export function InboxClient() {
  const [status, setStatus] = useState<ConversationStatus | "">("");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<Conversation[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => setQuery(search), 300); // debounce typing
    return () => clearTimeout(timer);
  }, [search]);

  const load = useCallback(async () => {
    try {
      setItems(await request<Conversation[]>(inboxQuery(status, query)));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [status, query]);

  useEffect(() => {
    setItems(null);
    void load();
  }, [load]);
  usePolling(load, 10_000);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div role="group" aria-label="Filter by status" className="flex flex-wrap gap-2">
          {FILTERS.map((f) => (
            <button
              key={f.value || "all"}
              type="button"
              aria-pressed={status === f.value}
              onClick={() => setStatus(f.value)}
              className={`rounded-pill px-4 py-2 text-sm font-semibold transition ${status === f.value ? "bg-ink text-white" : "bg-surface text-ink shadow-soft hover:bg-subtle"}`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="sm:w-72">
          <label htmlFor="inbox-search" className="sr-only">
            Search conversations
          </label>
          <input id="inbox-search" type="search" placeholder="Search names or messages" value={search} onChange={(e) => setSearch(e.target.value)} className={inputClass} />
        </div>
      </div>
      {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}
      {items === null && !error ? <Loading label="Loading conversations" /> : null}
      {items && items.length === 0 ? (
        <EmptyState title={query || status ? "No conversations match" : "No conversations yet"}>
          {query || status ? "Try another filter or search." : "Conversations appear here as soon as a customer writes."}
        </EmptyState>
      ) : null}
      {items && items.length > 0 ? (
        <ul className="flex flex-col gap-2" aria-label="Conversations">
          {items.map((c) => (
            <li key={c.id}>
              <Link href={`/inbox/${c.id}`} className="flex items-start gap-3 rounded-panel bg-surface p-4 shadow-soft transition hover:bg-subtle sm:p-5">
                <span aria-hidden="true" className={`mt-2 size-2.5 shrink-0 rounded-full ${c.unread ? "bg-violet" : "bg-transparent"}`} />
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className={`truncate ${c.unread ? "font-bold" : "font-semibold"}`}>{customerName(c)}</span>
                    {c.unread ? <span className="sr-only">(unread)</span> : null}
                    <Badge tone={STATUS_TONE[c.status]}>{STATUS_LABELS[c.status]}</Badge>
                    <Badge>{channelLabel(c.channel)}</Badge>
                  </span>
                  <span className="mt-1 block truncate text-sm text-muted">
                    {c.last_message_role === "user" ? "" : c.last_message_role === "staff" ? "Team: " : "Assistant: "}
                    {c.last_message_preview ?? "No messages"}
                  </span>
                </span>
                <span className="shrink-0 text-xs text-muted">{c.last_message_at ? relativeTime(c.last_message_at) : ""}</span>
              </Link>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

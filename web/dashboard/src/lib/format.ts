/** Formatting for people: dates, sizes, durations, money, statuses. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "–";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "–";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`;
}

export function formatUsd(value: number): string {
  if (value === 0) return "$0";
  if (value < 0.01) return "under $0.01";
  return `$${value.toFixed(2)}`;
}

export function formatPercent(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : `${Math.round(value * 100)}%`;
}

export function relativeTime(iso: string, now = Date.now()): string {
  const seconds = Math.round((now - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

export function formatDateTime(iso: string, timeZone?: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone,
  }).format(new Date(iso));
}

export const STATUS_LABELS: Record<string, string> = {
  ai: "Assistant",
  waiting_human: "Waiting for team",
  human: "With team",
  resolved: "Resolved",
};

export const OUTCOME_LABELS: Record<string, string> = {
  answered: "Answered",
  smalltalk: "Small talk",
  no_answer: "No answer",
  action: "Action",
  handoff: "Handoff",
  error: "Error",
};

export const ROLE_LABELS: Record<string, string> = {
  user: "Customer",
  assistant: "Assistant",
  staff: "Team member",
};

export function channelLabel(channel: string): string {
  return channel === "telegram" ? "Telegram" : "Website";
}

export function customerName(c: { customer_name: string | null; visitor_id: string }): string {
  if (c.customer_name) return c.customer_name;
  return c.visitor_id.startsWith("tg:") ? "Telegram customer" : "Website visitor";
}

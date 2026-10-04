/** A conversation transcript in the house style: grey customer bubbles, accent assistant
 * bubbles, violet team-member bubbles. All text is rendered as text (React escapes it). */
import Link from "next/link";
import { Badge } from "@/components/ui";
import { OUTCOME_LABELS, ROLE_LABELS, formatDateTime } from "@/lib/format";
import type { Citation, Message, ToolRecord } from "@/lib/types";

const BUBBLE: Record<Message["role"], string> = {
  user: "bg-subtle text-ink rounded-bl-md",
  assistant: "bg-accent text-ink rounded-br-md",
  staff: "bg-violet-soft text-violet-ink rounded-br-md",
};

export function sourceLabel(c: Citation): string {
  const meta = c.metadata ?? {};
  if (typeof meta.section === "string") return `${c.document_title} · ${String(meta.section).split(" > ").pop()}`;
  if (meta.row !== undefined) return `${c.document_title} · row ${String(meta.row)}`;
  if (meta.staff_message) return "Message from the team";
  return c.document_title;
}

export function SourceChips({ citations }: { citations: Citation[] }) {
  const seen = new Set<string>();
  const unique = citations.filter((c) => {
    const label = sourceLabel(c);
    if (seen.has(label)) return false;
    seen.add(label);
    return true;
  });
  if (!unique.length) return null;
  return (
    <ul aria-label="Sources" className="mt-2 flex flex-wrap gap-1.5">
      {unique.map((c) => (
        <li key={`${c.chunk_id}-${c.marker}`} title={c.snippet} className="max-w-full truncate rounded-pill bg-violet-soft px-3 py-1 text-xs font-semibold text-violet-ink">
          {sourceLabel(c)}
        </li>
      ))}
    </ul>
  );
}

export function ToolChips({ tools }: { tools: ToolRecord[] }) {
  if (!tools.length) return null;
  return (
    <ul aria-label="Tools used" className="mt-2 flex flex-wrap gap-1.5">
      {tools.map((t, i) => (
        <li key={`${t.tool}-${i}`} className="rounded-pill border border-subtle-hover bg-surface px-3 py-1 font-mono text-xs">
          {t.tool} · {t.status}
        </li>
      ))}
    </ul>
  );
}

export function MessageItem({ message }: { message: Message }) {
  const business = message.role !== "user";
  const tools = message.retrieval?.tools ?? [];
  return (
    <li className={`flex flex-col ${business ? "items-end" : "items-start"}`}>
      <div className="mb-1 flex items-center gap-2 text-xs text-muted">
        <span className="font-semibold text-ink-soft">{ROLE_LABELS[message.role]}</span>
        <time dateTime={message.created_at}>{formatDateTime(message.created_at)}</time>
      </div>
      <div className={`max-w-[85%] whitespace-pre-wrap break-words rounded-bubble px-4 py-3 text-[15px] leading-relaxed sm:max-w-[70%] ${BUBBLE[message.role]}`}>{message.content}</div>
      {message.role === "assistant" ? (
        <div className={`flex max-w-[85%] flex-col sm:max-w-[70%] ${business ? "items-end" : ""}`}>
          <SourceChips citations={message.citations ?? []} />
          <ToolChips tools={tools} />
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {message.outcome ? <Badge tone={message.outcome === "no_answer" || message.outcome === "error" ? "danger" : "neutral"}>{OUTCOME_LABELS[message.outcome] ?? message.outcome}</Badge> : null}
            <Link href={`/traces/${message.id}`} className="rounded-pill px-2 py-0.5 text-xs font-semibold text-violet-ink underline underline-offset-4">
              View trace
            </Link>
          </div>
        </div>
      ) : null}
    </li>
  );
}

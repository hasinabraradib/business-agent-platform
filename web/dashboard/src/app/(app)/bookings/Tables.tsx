"use client";
import Link from "next/link";
import { Badge, Button, Card, EmptyState } from "@/components/ui";
import { downloadCsv, toCsv } from "@/lib/csv";
import { formatDateTime } from "@/lib/format";
import type { Lead, Reservation } from "@/lib/types";

const STATUS_TONE: Record<string, "accent" | "danger" | "neutral" | "violet"> = {
  confirmed: "accent",
  new: "violet",
  contacted: "neutral",
  closed: "neutral",
  completed: "neutral",
  cancelled: "danger",
  no_show: "danger",
};

function Status({ value }: { value: string }) {
  return <Badge tone={STATUS_TONE[value] ?? "neutral"}>{value.replace("_", " ")}</Badge>;
}

function ConversationLink({ id }: { id: string | null }) {
  return id ? (
    <Link href={`/inbox/${id}`} className="font-semibold text-violet-ink underline underline-offset-4">
      Open
    </Link>
  ) : (
    <span className="text-muted">–</span>
  );
}

export const RESERVATION_COLUMNS = [
  { header: "Reference", value: (r: Reservation) => r.reference },
  { header: "Date", value: (r: Reservation) => r.local_date },
  { header: "Time", value: (r: Reservation) => String(r.local_time).slice(0, 5) },
  { header: "Party size", value: (r: Reservation) => r.party_size },
  { header: "Name", value: (r: Reservation) => r.name },
  { header: "Phone", value: (r: Reservation) => r.phone },
  { header: "Notes", value: (r: Reservation) => r.notes },
  { header: "Status", value: (r: Reservation) => r.status },
  { header: "Conversation", value: (r: Reservation) => r.conversation_id ?? "" },
  { header: "Booked at", value: (r: Reservation) => r.created_at },
];

export const LEAD_COLUMNS = [
  { header: "Name", value: (l: Lead) => l.name },
  { header: "Contact", value: (l: Lead) => l.contact },
  { header: "Interest", value: (l: Lead) => l.interest },
  { header: "Status", value: (l: Lead) => l.status },
  { header: "Conversation", value: (l: Lead) => l.conversation_id ?? "" },
  { header: "Received", value: (l: Lead) => l.created_at },
];

function Section({ id, title, count, onExport, children }: { id: string; title: string; count: number; onExport: () => void; children: React.ReactNode }) {
  return (
    <section aria-labelledby={id}>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 id={id} className="text-lg font-bold">
          {title} <span className="font-normal text-muted">({count})</span>
        </h2>
        <Button size="sm" tone="secondary" onClick={onExport} disabled={!count}>
          Export CSV
        </Button>
      </div>
      {children}
    </section>
  );
}

export function ReservationsTable({ rows }: { rows: Reservation[] }) {
  return (
    <Section id="bookings" title="Bookings" count={rows.length} onExport={() => downloadCsv("bookings.csv", toCsv(rows, RESERVATION_COLUMNS))}>
      {rows.length ? (
        <Card as="div" className="overflow-x-auto p-0">
          <table className="w-full min-w-[44rem] text-left text-sm">
            <caption className="sr-only">Bookings</caption>
            <thead className="text-xs uppercase tracking-wide text-muted">
              <tr>
                <th scope="col" className="px-5 py-3">When</th>
                <th scope="col" className="px-3 py-3">Guest</th>
                <th scope="col" className="px-3 py-3 text-right">People</th>
                <th scope="col" className="px-3 py-3">Reference</th>
                <th scope="col" className="px-3 py-3">Status</th>
                <th scope="col" className="px-5 py-3">Conversation</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-subtle">
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="px-5 py-3.5 font-semibold">
                    {r.local_date} · {String(r.local_time).slice(0, 5)}
                  </td>
                  <td className="px-3 py-3.5">
                    <p className="font-medium">{r.name}</p>
                    <p className="text-xs text-muted">{r.phone}</p>
                  </td>
                  <td className="px-3 py-3.5 text-right">{r.party_size}</td>
                  <td className="px-3 py-3.5 font-mono">{r.reference}</td>
                  <td className="px-3 py-3.5">
                    <Status value={r.status} />
                  </td>
                  <td className="px-5 py-3.5">
                    <ConversationLink id={r.conversation_id} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      ) : (
        <EmptyState title="No bookings yet">Bookings appear here when customers reserve a table through the assistant.</EmptyState>
      )}
    </Section>
  );
}

export function LeadsTable({ rows }: { rows: Lead[] }) {
  return (
    <Section id="leads" title="Leads" count={rows.length} onExport={() => downloadCsv("leads.csv", toCsv(rows, LEAD_COLUMNS))}>
      {rows.length ? (
        <Card as="div" className="overflow-x-auto p-0">
          <table className="w-full min-w-[40rem] text-left text-sm">
            <caption className="sr-only">Leads</caption>
            <thead className="text-xs uppercase tracking-wide text-muted">
              <tr>
                <th scope="col" className="px-5 py-3">Customer</th>
                <th scope="col" className="px-3 py-3">Interested in</th>
                <th scope="col" className="px-3 py-3">Status</th>
                <th scope="col" className="px-3 py-3">Received</th>
                <th scope="col" className="px-5 py-3">Conversation</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-subtle">
              {rows.map((l) => (
                <tr key={l.id}>
                  <td className="px-5 py-3.5">
                    <p className="font-semibold">{l.name}</p>
                    <p className="text-xs text-muted">{l.contact}</p>
                  </td>
                  <td className="px-3 py-3.5">{l.interest}</td>
                  <td className="px-3 py-3.5">
                    <Status value={l.status} />
                  </td>
                  <td className="px-3 py-3.5 text-muted">{formatDateTime(l.created_at)}</td>
                  <td className="px-5 py-3.5">
                    <ConversationLink id={l.conversation_id} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      ) : (
        <EmptyState title="No leads yet">When a customer asks to be contacted (bulk orders, events), their details appear here.</EmptyState>
      )}
    </Section>
  );
}

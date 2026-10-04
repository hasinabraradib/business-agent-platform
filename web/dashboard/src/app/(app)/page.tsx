import type { Metadata } from "next";
import Link from "next/link";
import { BarChart } from "@/components/BarChart";
import { Card, EmptyState, PageHeader } from "@/components/ui";
import { formatMs, formatPercent, formatUsd, OUTCOME_LABELS, relativeTime } from "@/lib/format";
import { api } from "@/lib/server";
import type { Overview } from "@/lib/types";

export const metadata: Metadata = { title: "Overview" };
export const dynamic = "force-dynamic";

const OUTCOME_TONES = { answered: "ink", smalltalk: "muted", no_answer: "danger", action: "accent", handoff: "violet", error: "danger" } as const;

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Card as="div" className="flex flex-col gap-1 p-5">
      <p className="text-sm text-muted">{label}</p>
      <p className="text-3xl font-bold tracking-tight">{value}</p>
      {hint ? <p className="text-xs text-muted">{hint}</p> : null}
    </Card>
  );
}

function dayLabel(day: string): string {
  return new Intl.DateTimeFormat("en-GB", { weekday: "short", timeZone: "UTC" }).format(new Date(`${day}T12:00:00Z`));
}

export default async function OverviewPage() {
  const data = await api<Overview>("/analytics/overview");
  const days = lastDays(data.daily, data.window_days);
  const outcomeBars = Object.entries(OUTCOME_LABELS)
    .filter(([key]) => key !== "error" || data.outcomes.error)
    .map(([key, label]) => ({ label, value: data.outcomes[key] ?? 0, tone: OUTCOME_TONES[key as keyof typeof OUTCOME_TONES] }));
  const replies = outcomeBars.reduce((sum, b) => sum + b.value, 0);
  return (
    <>
      <PageHeader title="Overview" description={`How your assistant did over the last ${data.window_days} days (${data.timezone}).`} />
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Stat label="Conversations today" value={String(data.conversations_today)} />
        <Stat label="This week" value={String(data.conversations_week)} hint="last 7 days" />
        <Stat label="Handed to your team" value={formatPercent(data.handoff_rate)} hint="of this week's conversations" />
        <Stat label="Median first reply" value={formatMs(data.median_first_token_ms)} hint="time to first word" />
        <Stat label="Estimated cost" value={formatUsd(data.estimated_cost_usd)} hint={`${(data.tokens.prompt + data.tokens.completion).toLocaleString()} tokens`} />
      </div>
      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card>
          <BarChart title="Conversations per day" orientation="vertical" bars={days.map((d) => ({ label: dayLabel(d.day), value: d.conversations, tone: "accent" }))} />
        </Card>
        <Card>
          {replies ? (
            <BarChart title="How replies ended" bars={outcomeBars} />
          ) : (
            <EmptyState title="No replies yet">Outcomes appear here once customers start chatting.</EmptyState>
          )}
        </Card>
      </div>
      <Card className="mt-6" aria-labelledby="gaps-heading">
        <div className="mb-4 flex items-center justify-between gap-3">
          <h2 id="gaps-heading" className="font-semibold">
            Latest questions without an answer
          </h2>
          <Link href="/gaps" className="rounded-pill bg-subtle px-4 py-2 text-sm font-semibold hover:bg-subtle-hover">
            All knowledge gaps
          </Link>
        </div>
        {data.recent_gaps.length ? (
          <ul className="divide-y divide-subtle">
            {data.recent_gaps.map((gap) => (
              <li key={gap.message_id} className="flex flex-wrap items-baseline justify-between gap-2 py-3">
                <Link href={`/inbox/${gap.conversation_id}`} className="font-medium underline-offset-4 hover:underline">
                  {gap.question}
                </Link>
                <span className="text-sm text-muted">{relativeTime(gap.asked_at)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState title="No gaps right now">Every recent question had an answer in your knowledge.</EmptyState>
        )}
      </Card>
    </>
  );
}

function lastDays(daily: Overview["daily"], count: number) {
  const byDay = new Map(daily.map((d) => [d.day, d.conversations]));
  const days = [];
  const end = daily.length ? new Date(`${daily[daily.length - 1].day}T12:00:00Z`) : new Date();
  const today = new Date();
  const last = end > today ? end : today;
  for (let i = count - 1; i >= 0; i--) {
    const day = new Date(last.getTime() - i * 86_400_000).toISOString().slice(0, 10);
    days.push({ day, conversations: byDay.get(day) ?? 0 });
  }
  return days;
}

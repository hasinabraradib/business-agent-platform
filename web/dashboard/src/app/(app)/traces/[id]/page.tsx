import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { SourceChips } from "@/components/Transcript";
import { Badge, Card, EmptyState, PageHeader } from "@/components/ui";
import { OUTCOME_LABELS, formatDateTime, formatMs } from "@/lib/format";
import { ApiError, api } from "@/lib/server";
import type { SearchTrace, Trace } from "@/lib/types";

export const metadata: Metadata = { title: "Run trace" };
export const dynamic = "force-dynamic";

function score(value: number | null): string {
  return value === null ? "–" : value.toFixed(3);
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-4">
      <dt className="w-40 shrink-0 text-sm text-muted">{label}</dt>
      <dd className="min-w-0 break-words font-medium">{children}</dd>
    </div>
  );
}

function SearchCard({ search, index }: { search: SearchTrace; index: number }) {
  return (
    <Card as="article" aria-label={`Search ${index + 1}`}>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h3 className="font-semibold">
          Search {index + 1}: <span className="font-mono text-[15px]">&ldquo;{search.query}&rdquo;</span>
        </h3>
        <Badge tone={search.decision === "passed" ? "accent" : "danger"}>{search.decision === "passed" ? "Passed the relevance check" : "Blocked: nothing relevant"}</Badge>
      </div>
      <dl className="mb-4 flex flex-col gap-2">
        <Row label="Mode">{search.mode || "–"}</Row>
        <Row label="Decision">{search.reason}</Row>
        <Row label="Reranker">{search.reranker ? `${search.reranker}${search.rerank_applied ? " (applied)" : " (not applied)"}` : "none"}</Row>
        <Row label="Time">{`${formatMs(search.duration_ms)}${search.embedding_cached ? ", embedding from cache" : ""}`}</Row>
      </dl>
      {search.results.length ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[40rem] text-left text-sm">
            <caption className="sr-only">Chunks returned by search {index + 1}, best first</caption>
            <thead className="text-xs uppercase tracking-wide text-muted">
              <tr>
                <th scope="col" className="py-2 pr-3">#</th>
                <th scope="col" className="py-2 pr-3">Source</th>
                <th scope="col" className="py-2 pr-3 text-right">Vector</th>
                <th scope="col" className="py-2 pr-3 text-right">Keyword</th>
                <th scope="col" className="py-2 pr-3 text-right">Fused</th>
                <th scope="col" className="py-2 text-right">Rerank</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-subtle">
              {search.results.map((r, i) => (
                <tr key={r.chunk_id} className="align-top">
                  <td className="py-2.5 pr-3 text-muted">{i + 1}</td>
                  <td className="py-2.5 pr-3">
                    <p className="font-semibold">
                      {r.document_title}
                      {r.location ? <span className="font-normal text-muted"> · {r.location}</span> : null}
                    </p>
                    <p className="mt-0.5 line-clamp-2 text-muted">{r.snippet}</p>
                  </td>
                  <td className="py-2.5 pr-3 text-right font-mono">{score(r.vector_score)}</td>
                  <td className="py-2.5 pr-3 text-right font-mono">{score(r.keyword_score)}</td>
                  <td className="py-2.5 pr-3 text-right font-mono">{score(r.fused_score)}</td>
                  <td className="py-2.5 text-right font-mono">{score(r.rerank_score)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-sm text-muted">This search returned no chunks.</p>
      )}
    </Card>
  );
}

export default async function TracePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let trace: Trace;
  try {
    trace = await api<Trace>(`/messages/${encodeURIComponent(id)}/trace`);
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.status === 422)) notFound();
    throw error;
  }
  const t = trace.timings_ms ?? {};
  return (
    <>
      <Link href={`/inbox/${trace.conversation_id}`} className="mb-4 inline-block rounded-pill px-2 py-1 text-sm font-semibold text-muted hover:text-ink">
        ← Back to the conversation
      </Link>
      <PageHeader title="Run trace" description={`Everything recorded for the assistant's reply on ${formatDateTime(trace.created_at)}.`} />
      <div className="grid gap-6 lg:grid-cols-[1fr_20rem]">
        <Card aria-labelledby="exchange">
          <h2 id="exchange" className="mb-4 font-semibold">
            Question and reply
          </h2>
          <p className="text-sm text-muted">Customer asked</p>
          <p className="mb-4 whitespace-pre-wrap rounded-bubble bg-subtle px-4 py-3">{trace.question ?? "–"}</p>
          <p className="text-sm text-muted">Assistant replied</p>
          <p className="whitespace-pre-wrap rounded-bubble bg-accent px-4 py-3">{trace.reply}</p>
          <SourceChips citations={trace.citations ?? []} />
        </Card>
        <Card aria-labelledby="run">
          <h2 id="run" className="mb-4 font-semibold">
            Run
          </h2>
          <dl className="flex flex-col gap-2">
            <Row label="Outcome">{trace.outcome ? (OUTCOME_LABELS[trace.outcome] ?? trace.outcome) : "–"}</Row>
            <Row label="Model">{trace.model ?? "–"}</Row>
            <Row label="Fallback">{trace.fallback ? `Yes, after ${trace.failovers.map((f) => f.model).join(", ")}` : "No"}</Row>
            <Row label="First token">{formatMs(t.first_token)}</Row>
            <Row label="Total">{formatMs(t.total)}</Row>
            <Row label="Model calls">{String(Object.keys(t).filter((k) => k.startsWith("model_")).length)}</Row>
            <Row label="Tokens">{`${trace.tokens.prompt ?? 0} in, ${trace.tokens.completion ?? 0} out`}</Row>
          </dl>
          {trace.error ? <p className="mt-4 rounded-2xl bg-danger-soft px-3 py-2 text-sm text-danger">{trace.error}</p> : null}
        </Card>
      </div>
      {trace.failovers.length ? (
        <Card className="mt-6" aria-labelledby="failovers">
          <h2 id="failovers" className="mb-3 font-semibold">
            Failovers
          </h2>
          <ul className="flex flex-col gap-2 text-sm">
            {trace.failovers.map((f, i) => (
              <li key={i}>
                <span className="font-mono font-semibold">{f.model}</span>: {f.error}
              </li>
            ))}
          </ul>
        </Card>
      ) : null}
      <section className="mt-8" aria-labelledby="searches">
        <h2 id="searches" className="mb-3 text-lg font-bold">
          Searches
        </h2>
        {trace.searches.length ? (
          <div className="flex flex-col gap-4">
            {trace.searches.map((s, i) => (
              <SearchCard key={i} search={s} index={i} />
            ))}
          </div>
        ) : (
          <EmptyState title="No search in this turn">The assistant answered without looking anything up (e.g. a greeting).</EmptyState>
        )}
      </section>
      <section className="mt-8" aria-labelledby="tools">
        <h2 id="tools" className="mb-3 text-lg font-bold">
          Tool calls
        </h2>
        {trace.tools.length ? (
          <div className="flex flex-col gap-4">
            {trace.tools.map((tool, i) => (
              <Card key={i} as="article" aria-label={`Tool call ${i + 1}: ${tool.tool}`}>
                <div className="mb-3 flex flex-wrap items-center gap-2">
                  <h3 className="font-mono font-semibold">{tool.tool}</h3>
                  <Badge tone={tool.status === "ok" ? "accent" : tool.status === "needs_confirmation" ? "violet" : "danger"}>{tool.status}</Badge>
                  <span className="text-sm text-muted">step {tool.step}</span>
                </div>
                <p className="text-sm text-muted">Arguments</p>
                <pre className="mb-3 overflow-x-auto rounded-2xl bg-subtle p-3 text-xs">{JSON.stringify(tool.arguments ?? {}, null, 2)}</pre>
                <p className="text-sm text-muted">Result the model saw</p>
                <pre className="overflow-x-auto whitespace-pre-wrap rounded-2xl bg-subtle p-3 text-xs">{tool.result || tool.summary || "–"}</pre>
              </Card>
            ))}
          </div>
        ) : (
          <EmptyState title="No tools used" />
        )}
      </section>
    </>
  );
}

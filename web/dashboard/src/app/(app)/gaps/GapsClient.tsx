"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Dialog } from "@/components/Dialog";
import { Badge, Button, Card, EmptyState, ErrorState, Field, Loading, inputClass } from "@/components/ui";
import { request } from "@/lib/client";
import { relativeTime } from "@/lib/format";
import type { GapGroup } from "@/lib/types";

export function GapsClient() {
  const [gaps, setGaps] = useState<GapGroup[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [answering, setAnswering] = useState<GapGroup | null>(null);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setGaps(await request<GapGroup[]>("knowledge-gaps"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  function open(gap: GapGroup) {
    setAnswering(gap);
    setQuestion(gap.question);
    setAnswer("");
    setFormError(null);
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!answering) return;
    setSaving(true);
    setFormError(null);
    try {
      await request("knowledge-gaps/answer", {
        method: "POST",
        body: JSON.stringify({ question: question.trim(), answer: answer.trim(), message_ids: answering.message_ids }),
      });
      setAnswering(null);
      setNotice(`Added to your knowledge: "${question.trim()}". It is being indexed now.`);
      await load();
    } catch (e) {
      setFormError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {notice ? (
        <p role="status" className="rounded-2xl bg-accent px-4 py-3 text-sm font-semibold">
          {notice}
        </p>
      ) : null}
      {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}
      {gaps === null && !error ? <Loading label="Loading knowledge gaps" /> : null}
      {gaps && gaps.length === 0 ? (
        <EmptyState title="No open gaps">Every question so far had an answer in your knowledge. New gaps show up here when the assistant can&apos;t answer.</EmptyState>
      ) : null}
      {gaps && gaps.length > 0 ? (
        <ul className="flex flex-col gap-3" aria-label="Knowledge gaps">
          {gaps.map((gap) => (
            <li key={gap.message_ids[0]}>
              <Card as="article" className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="font-semibold">{gap.question}</h2>
                    <Badge tone={gap.count > 1 ? "violet" : "neutral"}>{gap.count === 1 ? "asked once" : `asked ${gap.count} times`}</Badge>
                  </div>
                  <p className="mt-1 text-sm text-muted">Last asked {relativeTime(gap.last_asked_at)}</p>
                  {gap.examples.length > 1 ? (
                    <p className="mt-2 text-sm text-ink-soft">
                      Also asked as: {gap.examples.slice(1).map((e) => `“${e}”`).join(", ")}
                    </p>
                  ) : null}
                  <Link href={`/inbox/${gap.conversation_ids[0]}`} className="mt-2 inline-block text-sm font-semibold text-violet-ink underline underline-offset-4">
                    Open the conversation
                  </Link>
                </div>
                <Button tone="accent" onClick={() => open(gap)} className="shrink-0">
                  Add an answer
                </Button>
              </Card>
            </li>
          ))}
        </ul>
      ) : null}
      <Dialog open={answering !== null} title="Add an answer" onClose={() => setAnswering(null)}>
        <form onSubmit={save} className="flex flex-col gap-4">
          <Field label="Question" id="gap-question" hint="Edit it into the form customers would recognise.">
            <input id="gap-question" required minLength={3} maxLength={300} value={question} onChange={(e) => setQuestion(e.target.value)} className={inputClass} aria-describedby="gap-question-hint" />
          </Field>
          <Field label="Answer" id="gap-answer" hint="A short, factual answer. It becomes a knowledge entry the assistant can cite.">
            <textarea id="gap-answer" required minLength={3} maxLength={2000} rows={5} value={answer} onChange={(e) => setAnswer(e.target.value)} className={`${inputClass} resize-y`} aria-describedby="gap-answer-hint" />
          </Field>
          {formError ? (
            <p role="alert" className="text-sm font-medium text-danger">
              {formError}
            </p>
          ) : null}
          <div className="flex justify-end gap-2">
            <Button tone="secondary" onClick={() => setAnswering(null)}>
              Cancel
            </Button>
            <Button type="submit" disabled={saving || answer.trim().length < 3 || question.trim().length < 3}>
              {saving ? "Saving…" : "Save answer"}
            </Button>
          </div>
        </form>
      </Dialog>
    </div>
  );
}

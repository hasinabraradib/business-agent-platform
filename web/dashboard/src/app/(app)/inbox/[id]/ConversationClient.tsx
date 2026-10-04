"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { MessageItem } from "@/components/Transcript";
import { Badge, Button, Card, ErrorState, Loading } from "@/components/ui";
import { request } from "@/lib/client";
import { STATUS_LABELS, channelLabel, customerName } from "@/lib/format";
import type { ConversationDetail } from "@/lib/types";
import { usePolling } from "@/lib/usePolling";
import { STATUS_TONE } from "../InboxClient";

export function ConversationClient({ id }: { id: string }) {
  const [data, setData] = useState<ConversationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const count = useRef(0);

  const load = useCallback(async () => {
    try {
      const detail = await request<ConversationDetail>(`conversations/${id}`);
      setData(detail);
      setError(null);
      if (detail.messages.length !== count.current) {
        count.current = detail.messages.length;
        void request(`conversations/${id}/read`, { method: "POST" }).catch(() => undefined);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);
  usePolling(load, 10_000);
  const messageCount = data?.messages.length ?? 0;
  useEffect(() => {
    if (messageCount) endRef.current?.scrollIntoView({ block: "end" }); // new messages: show them
  }, [messageCount]);

  async function act(name: string, path: string, body?: object, success?: string) {
    setBusy(name);
    setNotice(null);
    try {
      await request(`conversations/${id}/${path}`, { method: "POST", body: body ? JSON.stringify(body) : undefined });
      if (success) setNotice(success);
      await load();
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function send(event: React.FormEvent) {
    event.preventDefault();
    const text = reply.trim();
    if (!text) return;
    if (await act("reply", "messages", { text }, "Reply sent. The assistant stays quiet until you hand back.")) setReply("");
  }

  if (!data && error) return <ErrorState message={error} onRetry={() => void load()} />;
  if (!data) return <Loading label="Loading the conversation" />;

  return (
    <div className="flex flex-col gap-4">
      <Link href="/inbox" className="self-start rounded-pill px-2 py-1 text-sm font-semibold text-muted hover:text-ink">
        ← Back to inbox
      </Link>
      <Card className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">{customerName(data)}</h1>
          <div className="mt-2 flex flex-wrap gap-2">
            <Badge tone={STATUS_TONE[data.status]}>{STATUS_LABELS[data.status]}</Badge>
            <Badge>{channelLabel(data.channel)}</Badge>
            <Badge>{data.message_count} messages</Badge>
          </div>
          {data.handoff_reason ? <p className="mt-3 text-sm text-muted">Handed over: {data.handoff_reason}</p> : null}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button tone="secondary" disabled={busy !== null || data.status === "ai"} onClick={() => void act("back", "hand-back", undefined, "Handed back: the assistant answers again.")}>
            Hand back to AI
          </Button>
          <Button tone="accent" disabled={busy !== null || data.status === "resolved"} onClick={() => void act("resolve", "resolve", undefined, "Marked resolved.")}>
            Mark resolved
          </Button>
        </div>
      </Card>
      {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}
      {notice ? (
        <p role="status" className="rounded-2xl bg-surface px-4 py-3 text-sm font-medium shadow-soft">
          {notice}
        </p>
      ) : null}
      <Card aria-label="Transcript">
        <ol className="flex flex-col gap-5">
          {data.messages.map((m) => (
            <MessageItem key={m.id} message={m} />
          ))}
        </ol>
        <div ref={endRef} />
      </Card>
      <Card as="div">
        <form onSubmit={send} className="flex flex-col gap-3">
          <label htmlFor="staff-reply" className="font-semibold">
            Reply as a team member
          </label>
          <textarea
            id="staff-reply"
            rows={3}
            maxLength={2000}
            value={reply}
            onChange={(e) => setReply(e.target.value)}
            placeholder={data.channel === "telegram" ? "Sent to the customer on Telegram" : "Shown in the customer's chat window"}
            className="w-full resize-y rounded-2xl border border-subtle-hover bg-surface px-4 py-3 text-[15px] placeholder:text-muted focus-visible:border-focus"
          />
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-muted">Replying pauses the assistant for this customer until you hand back.</p>
            <Button type="submit" disabled={busy !== null || !reply.trim()}>
              {busy === "reply" ? "Sending…" : "Send reply"}
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}

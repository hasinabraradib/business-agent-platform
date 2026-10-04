"use client";
import { useRouter } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { Dialog } from "@/components/Dialog";
import { Badge, Button, Card, EmptyState, ErrorState, Field, inputClass } from "@/components/ui";
import { request } from "@/lib/client";
import { formatDateTime } from "@/lib/format";
import type { ApiKey, Delivery } from "@/lib/types";

type Settings = Record<string, unknown>;

export const WEEKDAYS = [
  ["mon", "Monday"],
  ["tue", "Tuesday"],
  ["wed", "Wednesday"],
  ["thu", "Thursday"],
  ["fri", "Friday"],
  ["sat", "Saturday"],
  ["sun", "Sunday"],
] as const;

export const TOOLS = [
  ["search_knowledge", "Answer from your knowledge", "Search your documents to answer questions."],
  ["query_catalog", "Browse the menu or catalogue", "Filter items by price, category or allergens."],
  ["create_reservation", "Book tables", "Take bookings after the customer confirms."],
  ["lookup_order", "Look up orders", "Order status with the order number and phone digits."],
  ["capture_lead", "Take enquiries", "Save a customer's details for the team to call back."],
  ["request_human", "Hand over to the team", "Pass a conversation to a person when needed."],
] as const;

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** One settings section with its own save button and status line. */
function SettingsCard({ id, title, description, children, onSave, saving, message }: { id: string; title: string; description?: string; children: ReactNode; onSave?: () => void; saving?: boolean; message?: { ok: boolean; text: string } | null }) {
  return (
    <Card aria-labelledby={`${id}-title`} className="scroll-mt-6">
      <div id={id} className="mb-5">
        <h2 id={`${id}-title`} className="text-lg font-bold">
          {title}
        </h2>
        {description ? <p className="mt-1 text-sm text-muted">{description}</p> : null}
      </div>
      {onSave ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onSave();
          }}
          className="flex flex-col gap-4"
        >
          {children}
          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </Button>
            {message ? (
              <p role={message.ok ? "status" : "alert"} className={`text-sm font-medium ${message.ok ? "text-ink-soft" : "text-danger"}`}>
                {message.text}
              </p>
            ) : null}
          </div>
        </form>
      ) : (
        <div className="flex flex-col gap-4">{children}</div>
      )}
    </Card>
  );
}

function useSettingsSave() {
  const router = useRouter();
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const save = useCallback(
    async (patch: Settings) => {
      setSaving(true);
      setMessage(null);
      try {
        await request("tenant/settings", { method: "PATCH", body: JSON.stringify(patch) });
        setMessage({ ok: true, text: "Saved." });
        router.refresh();
      } catch (e) {
        setMessage({ ok: false, text: (e as Error).message });
      } finally {
        setSaving(false);
      }
    },
    [router],
  );
  return { saving, message, save };
}

export function BusinessSection({ settings, tenantName }: { settings: Settings; tenantName: string }) {
  const { saving, message, save } = useSettingsSave();
  const [form, setForm] = useState({
    business_name: str(settings.business_name) || tenantName,
    assistant_name: str(settings.assistant_name),
    fallback_contact: str(settings.fallback_contact),
    follow_up_promise: str(settings.follow_up_promise),
    tone: str(settings.tone),
    instructions: str(settings.instructions),
  });
  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setForm({ ...form, [key]: e.target.value });
  return (
    <SettingsCard id="business" title="Business" description="How the assistant introduces your business and how customers reach a person." onSave={() => void save(form)} saving={saving} message={message}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Business name" id="business-name">
          <input id="business-name" required maxLength={120} value={form.business_name} onChange={set("business_name")} className={inputClass} />
        </Field>
        <Field label="Assistant's name" id="assistant-name">
          <input id="assistant-name" maxLength={60} value={form.assistant_name} onChange={set("assistant_name")} className={inputClass} />
        </Field>
        <Field label="Contact line" id="contact" hint="Offered when the assistant can't help, e.g. a phone number and hours.">
          <input id="contact" maxLength={200} value={form.fallback_contact} onChange={set("fallback_contact")} className={inputClass} aria-describedby="contact-hint" />
        </Field>
        <Field label="Reply-time promise" id="promise" hint="e.g. “within one working day”. Leave empty to promise no time.">
          <input id="promise" maxLength={200} value={form.follow_up_promise} onChange={set("follow_up_promise")} className={inputClass} aria-describedby="promise-hint" />
        </Field>
      </div>
      <Field label="Tone" id="tone" hint="A few words, e.g. “warm, friendly and concise”.">
        <input id="tone" maxLength={200} value={form.tone} onChange={set("tone")} className={inputClass} aria-describedby="tone-hint" />
      </Field>
      <Field label="Notes for the assistant" id="instructions" hint="Extra guidance, e.g. “Mention the Friday lunch special when relevant.”">
        <textarea id="instructions" rows={3} maxLength={1000} value={form.instructions} onChange={set("instructions")} className={`${inputClass} resize-y`} aria-describedby="instructions-hint" />
      </Field>
    </SettingsCard>
  );
}

export function hoursToForm(raw: unknown): Record<string, { closed: boolean; open: string; close: string }> {
  const hours = (raw && typeof raw === "object" ? raw : {}) as Record<string, [string, string][]>;
  return Object.fromEntries(
    WEEKDAYS.map(([key]) => {
      const first = hours[key]?.[0];
      return [key, first ? { closed: false, open: first[0], close: first[1] } : { closed: true, open: "10:00", close: "20:00" }];
    }),
  );
}

export function formToHours(form: ReturnType<typeof hoursToForm>, raw: unknown): Record<string, [string, string][]> {
  const previous = (raw && typeof raw === "object" ? raw : {}) as Record<string, [string, string][]>;
  const out: Record<string, [string, string][]> = {};
  for (const [key] of WEEKDAYS) {
    const day = form[key];
    if (day.closed) continue;
    // The form edits the first opening of each day; any later ones (split shifts) are kept.
    out[key] = [[day.open, day.close], ...(previous[key]?.slice(1) ?? [])];
  }
  return out;
}

export function HoursSection({ settings }: { settings: Settings }) {
  const { saving, message, save } = useSettingsSave();
  const [timezone, setTimezone] = useState(str(settings.timezone) || "UTC");
  const [days, setDays] = useState(() => hoursToForm(settings.opening_hours));
  const update = (key: string, patch: Partial<{ closed: boolean; open: string; close: string }>) => setDays({ ...days, [key]: { ...days[key], ...patch } });
  return (
    <SettingsCard
      id="hours"
      title="Opening hours and timezone"
      description="Used for bookings, for “are you open now?”, and to tell customers when the team is back. A closing time before the opening time means after midnight."
      onSave={() => void save({ timezone, opening_hours: formToHours(days, settings.opening_hours) })}
      saving={saving}
      message={message}
    >
      <Field label="Timezone" id="timezone" hint="An IANA name, e.g. Asia/Dhaka.">
        <input id="timezone" list="timezones" value={timezone} onChange={(e) => setTimezone(e.target.value)} className={`${inputClass} sm:max-w-xs`} aria-describedby="timezone-hint" />
      </Field>
      <datalist id="timezones">
        {["Asia/Dhaka", "Asia/Kolkata", "Asia/Dubai", "Europe/London", "UTC", "America/New_York"].map((tz) => (
          <option key={tz} value={tz}>
            {tz}
          </option>
        ))}
      </datalist>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-2 text-sm font-semibold">Weekly hours</legend>
        {WEEKDAYS.map(([key, label]) => (
          <div key={key} className="grid grid-cols-[6.5rem_auto] items-center gap-3 sm:grid-cols-[7rem_7rem_1fr]">
            <span className="font-medium">{label}</span>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={days[key].closed} onChange={(e) => update(key, { closed: e.target.checked })} className="size-4 accent-[#111]" />
              Closed
            </label>
            {!days[key].closed ? (
              <div className="col-span-2 flex items-center gap-2 sm:col-span-1">
                <label className="sr-only" htmlFor={`${key}-open`}>{`${label} opens`}</label>
                <input id={`${key}-open`} type="time" value={days[key].open} onChange={(e) => update(key, { open: e.target.value })} className={`${inputClass} w-32`} />
                <span aria-hidden="true">–</span>
                <label className="sr-only" htmlFor={`${key}-close`}>{`${label} closes`}</label>
                <input id={`${key}-close`} type="time" value={days[key].close} onChange={(e) => update(key, { close: e.target.value })} className={`${inputClass} w-32`} />
              </div>
            ) : null}
          </div>
        ))}
      </fieldset>
    </SettingsCard>
  );
}

export function HandoffSection({ settings }: { settings: Settings }) {
  const { saving, message, save } = useSettingsSave();
  const [online, setOnline] = useState(str(settings.handoff_online_message));
  const [offline, setOffline] = useState(str(settings.handoff_offline_message));
  return (
    <SettingsCard
      id="handoff"
      title="Handoff messages"
      description="What a customer is told when the conversation is passed to your team. Leave empty for the built-in messages in the customer's language. {when} is when you're back; {reply_time} is your reply-time promise."
      onSave={() => void save({ handoff_online_message: online, handoff_offline_message: offline })}
      saving={saving}
      message={message}
    >
      <Field label="When the team is online" id="handoff-online">
        <textarea id="handoff-online" rows={2} maxLength={300} value={online} onChange={(e) => setOnline(e.target.value)} placeholder="I've passed this to our team. They're online now and will reply here soon." className={`${inputClass} resize-y`} />
      </Field>
      <Field label="When the team is offline" id="handoff-offline">
        <textarea id="handoff-offline" rows={2} maxLength={300} value={offline} onChange={(e) => setOffline(e.target.value)} placeholder="We're closed now and back {when}; we'll reply {reply_time}." className={`${inputClass} resize-y`} />
      </Field>
    </SettingsCard>
  );
}

export function ToolsSection({ settings }: { settings: Settings }) {
  const { saving, message, save } = useSettingsSave();
  const [enabled, setEnabled] = useState<string[]>(Array.isArray(settings.enabled_tools) ? (settings.enabled_tools as string[]) : ["search_knowledge"]);
  const toggle = (tool: string) => setEnabled(enabled.includes(tool) ? enabled.filter((t) => t !== tool) : [...enabled, tool]);
  return (
    <SettingsCard id="tools" title="What the assistant can do" description="Only the tools ticked here are offered to it." onSave={() => void save({ enabled_tools: enabled })} saving={saving} message={message}>
      <fieldset className="grid gap-3 sm:grid-cols-2">
        <legend className="sr-only">Enabled tools</legend>
        {TOOLS.map(([tool, label, hint]) => (
          <label key={tool} className={`flex cursor-pointer items-start gap-3 rounded-2xl border p-4 ${enabled.includes(tool) ? "border-ink bg-accent/30" : "border-subtle-hover"}`}>
            <input type="checkbox" checked={enabled.includes(tool)} onChange={() => toggle(tool)} className="mt-1 size-4 accent-[#111]" />
            <span>
              <span className="block font-semibold">{label}</span>
              <span className="block text-sm text-muted">{hint}</span>
            </span>
          </label>
        ))}
      </fieldset>
    </SettingsCard>
  );
}

export function parseOrigins(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((o) => o.trim())
    .filter(Boolean);
}

export function WebsiteSection({ settings }: { settings: Settings }) {
  const { saving, message, save } = useSettingsSave();
  const [origins, setOrigins] = useState((Array.isArray(settings.allowed_origins) ? (settings.allowed_origins as string[]) : []).join("\n"));
  return (
    <SettingsCard id="website" title="Website" description="The websites allowed to show your chat widget." onSave={() => void save({ allowed_origins: parseOrigins(origins) })} saving={saving} message={message}>
      <Field label="Allowed website origins" id="origins" hint="One per line, e.g. https://www.example.com">
        <textarea id="origins" rows={3} value={origins} onChange={(e) => setOrigins(e.target.value)} className={`${inputClass} resize-y font-mono text-sm`} aria-describedby="origins-hint" />
      </Field>
    </SettingsCard>
  );
}

export function WebhookSection({ initial }: { initial: { url: string; enabled: boolean; secret_hint: string } | null }) {
  const [endpoint, setEndpoint] = useState(initial);
  const [url, setUrl] = useState(initial?.url ?? "");
  const [secret, setSecret] = useState<string | null>(null);
  const [deliveries, setDeliveries] = useState<Delivery[] | null>(null);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const loadDeliveries = useCallback(async () => {
    try {
      setDeliveries(await request<Delivery[]>("webhooks/deliveries"));
    } catch {
      setDeliveries([]);
    }
  }, []);
  useEffect(() => {
    void loadDeliveries();
  }, [loadDeliveries]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setMessage(null);
    try {
      await action();
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <SettingsCard id="webhook" title="Webhook" description="We POST signed events (bookings, leads, handoffs) to this URL. See the README for the signature format.">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            const saved = await request<{ url: string; enabled: boolean; secret_hint: string; secret?: string | null }>("webhooks/endpoint", { method: "PUT", body: JSON.stringify({ url: url.trim(), enabled: true }) });
            setEndpoint(saved);
            if (saved.secret) setSecret(saved.secret);
            setMessage({ ok: true, text: "Saved." });
          });
        }}
        className="flex flex-col gap-3 sm:flex-row sm:items-end"
      >
        <div className="flex-1">
          <Field label="Webhook URL" id="webhook-url">
            <input id="webhook-url" type="url" required value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://hooks.example.com/bookings" className={inputClass} />
          </Field>
        </div>
        <Button type="submit" disabled={busy || !url.trim()}>
          Save URL
        </Button>
      </form>
      {endpoint ? (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted">Signing secret starts with</span>
          <code className="rounded-pill bg-subtle px-2 py-0.5">{endpoint.secret_hint}…</code>
          <Button
            size="sm"
            tone="secondary"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const rotated = await request<{ secret?: string | null; secret_hint: string; url: string; enabled: boolean }>("webhooks/endpoint/rotate-secret", { method: "POST" });
                setEndpoint(rotated);
                if (rotated.secret) setSecret(rotated.secret);
              })
            }
          >
            Rotate secret
          </Button>
          <Button
            size="sm"
            tone="accent"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await request("webhooks/test", { method: "POST" });
                setMessage({ ok: true, text: "Test event queued. It appears in the log below within a few seconds." });
                setTimeout(() => void loadDeliveries(), 2500);
              })
            }
          >
            Send test event
          </Button>
        </div>
      ) : null}
      {message ? (
        <p role={message.ok ? "status" : "alert"} className={`text-sm font-medium ${message.ok ? "text-ink-soft" : "text-danger"}`}>
          {message.text}
        </p>
      ) : null}
      <div>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="font-semibold">Delivery log</h3>
          <Button size="sm" tone="ghost" onClick={() => void loadDeliveries()}>
            Refresh
          </Button>
        </div>
        {deliveries && deliveries.length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[36rem] text-left text-sm">
              <caption className="sr-only">Webhook deliveries</caption>
              <thead className="text-xs uppercase tracking-wide text-muted">
                <tr>
                  <th scope="col" className="py-2 pr-3">Event</th>
                  <th scope="col" className="py-2 pr-3">Status</th>
                  <th scope="col" className="py-2 pr-3 text-right">Attempts</th>
                  <th scope="col" className="py-2 pr-3">Last result</th>
                  <th scope="col" className="py-2">Sent</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-subtle">
                {deliveries.slice(0, 20).map((d) => (
                  <tr key={d.id}>
                    <td className="py-2.5 pr-3 font-mono">{d.event_type}</td>
                    <td className="py-2.5 pr-3">
                      <Badge tone={d.status === "delivered" ? "accent" : d.status === "failed" ? "danger" : "neutral"}>{d.status}</Badge>
                    </td>
                    <td className="py-2.5 pr-3 text-right">{d.attempts}</td>
                    <td className="max-w-xs truncate py-2.5 pr-3 text-muted">{d.last_status_code ? `HTTP ${d.last_status_code}` : (d.last_error ?? "–")}</td>
                    <td className="py-2.5 text-muted">{formatDateTime(d.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-muted">{deliveries === null ? "Loading…" : "No deliveries yet."}</p>
        )}
      </div>
      <Dialog open={secret !== null} title="Your signing secret" onClose={() => setSecret(null)}>
        <p className="text-sm">Copy it now into your receiver (e.g. n8n). It won&apos;t be shown again.</p>
        <code className="break-all rounded-2xl bg-subtle p-3 font-mono text-sm">{secret}</code>
        <div className="flex justify-end gap-2">
          <Button tone="secondary" onClick={() => secret && void navigator.clipboard?.writeText(secret)}>
            Copy
          </Button>
          <Button onClick={() => setSecret(null)}>Done</Button>
        </div>
      </Dialog>
    </SettingsCard>
  );
}

interface TelegramInfo {
  configured: boolean;
  bot_username: string | null;
  staff_chat_id: number | null;
  webhook_registered: boolean;
}

export function TelegramSection({ initial }: { initial: TelegramInfo }) {
  const router = useRouter();
  const [info, setInfo] = useState(initial);
  const [token, setToken] = useState("");
  const [chatId, setChatId] = useState(initial.staff_chat_id?.toString() ?? "");
  const [publicUrl, setPublicUrl] = useState("");
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setMessage(null);
    try {
      await action();
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <SettingsCard id="telegram" title="Telegram" description="Let customers chat with your bot on Telegram, and get handoff alerts in your staff chat.">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={info.configured ? "accent" : "neutral"}>{info.configured ? `Connected${info.bot_username ? ` as @${info.bot_username}` : ""}` : "Not connected"}</Badge>
        {info.configured ? <Badge tone={info.webhook_registered ? "accent" : "danger"}>{info.webhook_registered ? "Webhook registered" : "Webhook not registered"}</Badge> : null}
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            const body: Record<string, unknown> = { staff_chat_id: chatId.trim() ? Number(chatId) : null };
            if (token.trim()) body.bot_token = token.trim();
            setInfo(await request<TelegramInfo>("channels/telegram", { method: "PUT", body: JSON.stringify(body) }));
            setToken(""); // never kept or shown again
            setMessage({ ok: true, text: token.trim() ? "Saved. The token is stored encrypted and won't be shown again." : "Saved." });
            router.refresh();
          });
        }}
        className="grid gap-4 sm:grid-cols-2"
      >
        <Field label="Bot token" id="bot-token" hint={info.configured ? "A token is saved. Enter a new one only to replace it." : "From @BotFather."}>
          <input id="bot-token" type="password" autoComplete="off" spellCheck={false} value={token} onChange={(e) => setToken(e.target.value)} placeholder={info.configured ? "••••••••" : "123456:ABC…"} className={inputClass} aria-describedby="bot-token-hint" />
        </Field>
        <Field label="Staff chat id" id="staff-chat" hint="Where handoff alerts go; group ids are negative.">
          <input id="staff-chat" inputMode="numeric" pattern="-?[0-9]*" value={chatId} onChange={(e) => setChatId(e.target.value)} className={inputClass} aria-describedby="staff-chat-hint" />
        </Field>
        <div className="flex flex-wrap gap-2 sm:col-span-2">
          <Button type="submit" disabled={busy || (!info.configured && !token.trim())}>
            Save
          </Button>
          <Button
            tone="secondary"
            disabled={busy || !info.configured}
            onClick={() =>
              void run(async () => {
                setStatus(await request<Record<string, unknown>>("channels/telegram/status"));
              })
            }
          >
            Check status
          </Button>
        </div>
      </form>
      {status ? (
        <dl className="grid gap-1 rounded-2xl bg-subtle p-4 text-sm sm:grid-cols-2">
          <dt className="text-muted">Bot token works</dt>
          <dd className="font-semibold">{status.bot_ok ? "Yes" : "No"}</dd>
          <dt className="text-muted">Webhook</dt>
          <dd className="break-all font-semibold">{(status.webhook_url as string) ?? "not set"}</dd>
          <dt className="text-muted">Updates waiting</dt>
          <dd className="font-semibold">{String(status.pending_updates ?? "–")}</dd>
          <dt className="text-muted">Last error from Telegram</dt>
          <dd className="font-semibold">{(status.last_error as string) ?? "none"}</dd>
        </dl>
      ) : null}
      {info.configured ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              await request("channels/telegram/webhook", { method: "POST", body: JSON.stringify({ public_base_url: publicUrl.trim() }) });
              setInfo({ ...info, webhook_registered: true });
              setMessage({ ok: true, text: "Webhook registered with Telegram." });
            });
          }}
          className="flex flex-col gap-3 sm:flex-row sm:items-end"
        >
          <div className="flex-1">
            <Field label="Public address of the platform API" id="public-url" hint="Telegram only calls https addresses.">
              <input id="public-url" type="url" required placeholder="https://api.example.com" value={publicUrl} onChange={(e) => setPublicUrl(e.target.value)} className={inputClass} aria-describedby="public-url-hint" />
            </Field>
          </div>
          <Button type="submit" tone="secondary" disabled={busy || !publicUrl.trim()}>
            Register webhook
          </Button>
        </form>
      ) : null}
      {message ? (
        <p role={message.ok ? "status" : "alert"} className={`text-sm font-medium ${message.ok ? "text-ink-soft" : "text-danger"}`}>
          {message.text}
        </p>
      ) : null}
    </SettingsCard>
  );
}

export function maskKey(key: ApiKey): string {
  return `${key.prefix}…`;
}

export function ApiKeysSection({ initial }: { initial: ApiKey[] }) {
  const [keys, setKeys] = useState(initial);
  const [kind, setKind] = useState<"admin" | "widget">("widget");
  const [label, setLabel] = useState("");
  const [created, setCreated] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function reload() {
    setKeys(await request<ApiKey[]>("api-keys"));
  }

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const result = await request<ApiKey & { key: string }>("api-keys", { method: "POST", body: JSON.stringify({ kind, label: label.trim() }) });
      setCreated(result.key);
      setLabel("");
      await reload();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function revoke(key: ApiKey) {
    setError(null);
    try {
      await request(`api-keys/${key.id}`, { method: "DELETE" });
      setRevoking(null);
      await reload();
    } catch (e) {
      setError((e as Error).message);
      setRevoking(null);
    }
  }

  const active = keys.filter((k) => !k.revoked_at);
  return (
    <SettingsCard id="keys" title="API keys" description="Widget keys go on your website; admin keys sign in here and manage your data. Keys are shown once, when created.">
      <form onSubmit={create} className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <Field label="Kind" id="key-kind">
          <select id="key-kind" value={kind} onChange={(e) => setKind(e.target.value as "admin" | "widget")} className={`${inputClass} sm:w-40`}>
            <option value="widget">Widget</option>
            <option value="admin">Admin</option>
          </select>
        </Field>
        <div className="flex-1">
          <Field label="Label" id="key-label">
            <input id="key-label" maxLength={200} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Main website" className={inputClass} />
          </Field>
        </div>
        <Button type="submit">Create key</Button>
      </form>
      {error ? <ErrorState message={error} /> : null}
      {active.length ? (
        <ul className="divide-y divide-subtle" aria-label="Active keys">
          {active.map((key) => (
            <li key={key.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <code className="rounded-pill bg-subtle px-2.5 py-0.5 text-sm">{maskKey(key)}</code>
                <Badge tone={key.kind === "admin" ? "ink" : "neutral"}>{key.kind}</Badge>
                <span className="text-sm">{key.label || <span className="text-muted">No label</span>}</span>
                <span className="text-xs text-muted">created {formatDateTime(key.created_at)}</span>
              </div>
              <Button size="sm" tone="danger" onClick={() => setRevoking(key)} aria-label={`Revoke ${key.label || key.prefix}`}>
                Revoke
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState title="No active keys" />
      )}
      <Dialog open={created !== null} title="Your new key" onClose={() => setCreated(null)}>
        <p className="text-sm">Copy it now. For your security it won&apos;t be shown again.</p>
        <code className="break-all rounded-2xl bg-subtle p-3 font-mono text-sm">{created}</code>
        <div className="flex justify-end gap-2">
          <Button tone="secondary" onClick={() => created && void navigator.clipboard?.writeText(created)}>
            Copy
          </Button>
          <Button onClick={() => setCreated(null)}>Done</Button>
        </div>
      </Dialog>
      <Dialog open={revoking !== null} title="Revoke this key?" onClose={() => setRevoking(null)}>
        <p>
          <code>{revoking ? maskKey(revoking) : ""}</code> stops working immediately. If it is the key you signed in with, you&apos;ll be signed out.
        </p>
        <div className="flex justify-end gap-2">
          <Button tone="secondary" onClick={() => setRevoking(null)}>
            Cancel
          </Button>
          <Button tone="danger" onClick={() => revoking && void revoke(revoking)}>
            Revoke key
          </Button>
        </div>
      </Dialog>
    </SettingsCard>
  );
}

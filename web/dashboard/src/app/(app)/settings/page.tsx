import type { Metadata } from "next";
import { PageHeader } from "@/components/ui";
import { ApiError, api } from "@/lib/server";
import type { ApiKey } from "@/lib/types";
import {
  ApiKeysSection,
  BusinessSection,
  HandoffSection,
  HoursSection,
  TelegramSection,
  ToolsSection,
  WebhookSection,
  WebsiteSection,
} from "./Sections";

export const metadata: Metadata = { title: "Settings" };
export const dynamic = "force-dynamic";

async function optional<T>(path: string): Promise<T | null> {
  try {
    return await api<T>(path);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

export default async function SettingsPage() {
  const [tenant, webhook, telegram, keys] = await Promise.all([
    api<{ name: string; settings: Record<string, unknown> }>("/tenant"),
    optional<{ url: string; enabled: boolean; secret_hint: string }>("/webhooks/endpoint"),
    api<{ configured: boolean; bot_username: string | null; staff_chat_id: number | null; webhook_registered: boolean }>("/channels/telegram"),
    api<ApiKey[]>("/api-keys"),
  ]);
  const settings = tenant.settings ?? {};
  return (
    <>
      <PageHeader title="Settings" description="Each section saves on its own." />
      <nav aria-label="Settings sections" className="mb-6 flex flex-wrap gap-2">
        {[
          ["business", "Business"],
          ["hours", "Hours"],
          ["handoff", "Handoff"],
          ["tools", "Tools"],
          ["website", "Website"],
          ["webhook", "Webhook"],
          ["telegram", "Telegram"],
          ["keys", "API keys"],
        ].map(([id, label]) => (
          <a key={id} href={`#${id}`} className="rounded-pill bg-surface px-4 py-2 text-sm font-semibold shadow-soft hover:bg-subtle">
            {label}
          </a>
        ))}
      </nav>
      <div className="flex flex-col gap-6">
        <BusinessSection settings={settings} tenantName={tenant.name} />
        <HoursSection settings={settings} />
        <HandoffSection settings={settings} />
        <ToolsSection settings={settings} />
        <WebsiteSection settings={settings} />
        <WebhookSection initial={webhook} />
        <TelegramSection initial={telegram} />
        <ApiKeysSection initial={keys} />
      </div>
    </>
  );
}

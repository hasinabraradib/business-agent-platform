import type { Metadata } from "next";
import { PageHeader } from "@/components/ui";
import { InboxClient } from "./InboxClient";

export const metadata: Metadata = { title: "Inbox" };

export default function InboxPage() {
  return (
    <>
      <PageHeader title="Inbox" description="Every conversation with your customers, from the website and Telegram." />
      <InboxClient />
    </>
  );
}

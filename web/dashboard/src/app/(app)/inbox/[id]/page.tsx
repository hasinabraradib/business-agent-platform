import type { Metadata } from "next";
import { ConversationClient } from "./ConversationClient";

export const metadata: Metadata = { title: "Conversation" };

export default async function ConversationPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ConversationClient id={id} />;
}
